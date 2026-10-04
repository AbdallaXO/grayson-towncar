# Stage 1 — Foundation: Implementation Plan (rev 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store each driver's regular shift and hard limits to the minute, including shifts that cross midnight. Add a switch that lets those regular shifts replace the hard-coded "observed-history" driver windows.

**Architecture:**
- The rules door (`dispatching/feasibility_guards.py`) learns minute-precision windows that can cross midnight. These ride as optional `start_min`/`end_min`/`kind` keys and are checked base → base: the drive to the pickup, the pickup buffer, the fuel stop, the wash and the report time all come from `handoff_chain`. Hour windows behave exactly as before.
- Driver facts live on `Driver`. Regular shifts live in new fields on `DriverWeeklySchedule` that point at a new `ShiftTemplate`. Their logic lives in a new module, `drivers/regular_shifts.py`.
- The availability door (`drivers/availability.py`) turns a confirmed regular shift into a 12-hour window only when `SchedulerSettings.regular_shift_windows` is on. The engine's window builders pass that window through.
- A profile card and three staff pages let managers set it all up.

**Tech Stack:** Django 5.1.4, Python 3.13, SQLite (dev and tests), Postgres (production), Bootstrap 5.3, server-rendered templates.

**Spec:** [07_STRUCTURED_SHIFTS_DESIGN.md](07_STRUCTURED_SHIFTS_DESIGN.md), especially:
- §2 rows U1, U2, U3, U5, U11, U14, U16, U19, C2, C3, C7, C8, C10, C11
- §3
- §6.1
- §8
- §10 (Stage 1)
- §12

**Worktree:** `C:/Users/abdia/OneDrive/Desktop/grayson-towncar/.claude/worktrees/structured-shifts-stage1`, branch `worktree-structured-shifts-stage1`. Do all work here. Never touch the main checkout; it holds the owner's uncommitted work.

**Scratchpad (SP):** `C:/Users/abdia/AppData/Local/Temp/claude/C--Users-abdia-OneDrive-Desktop-grayson-towncar/2431fe51-eacf-49ef-9f27-a80c965f0151/scratchpad`. It holds:
- `snapshot-2026-10-04.sqlite3` (frozen copy of the dev DB)
- `14_pipeline_parity_stage1_before.json` (parity baseline, captured before any change; a second capture on unchanged code showed 0 differences)

---

## Stage 1 decisions (made under delegation; the founder can overturn any)

| # | Decision |
|---|---|
| S1 | A regular shift is three new fields on each `DriverWeeklySchedule` row: `shift_template` (null means Off), `shift_start` and `shift_end`. An end at or before the start means the next day. New code never changes the legacy fields (`is_available`, `start_hour`, `end_hour`, `flexible`, `shift_type`, …) on an existing row. A row that `save_regular_shift` has to create copies the driver's `default_*` values, so the legacy reading does not move. (C11's "unwritten" fields are `DriverVehicleAssignment.planned_start_hour/end_hour`; nothing here writes them.) |
| S2 | Confirmation is stored on `Driver` (`regular_shift_confirmed_at`, `regular_shift_confirmed_by`). A week where every day is Off is a valid confirmed shift, e.g. a driver who takes extra shifts only. |
| S3 | The switch is `SchedulerSettings.regular_shift_windows` (default False). It can only be turned on when "drivers without a regular shift" is empty. It can always be turned off. Only the Regular Shifts page writes it: the generic settings endpoint and "Reset to defaults" never touch it. |
| S4 | **With the switch on**, a confirmed working day becomes a fixed, non-flexible window that is 12 hours wide on the edge that does not move (U11, C10: the handover edge floats):<br>• **Morning** is fixed at its start: `(start, start + max_span)`.<br>• **Evening** is fixed at its end: `(end − max_span, end)`.<br>• **Midday** keeps both of its typed times.<br>The window is then clipped by the hard limits. A confirmed Off day means unavailable. Approved time off still wins. A flexible exception falls back to today's behaviour. The stub is skipped for that driver; `USE_STUB_WINDOWS` itself stays True in Stage 1, and the switch acts as a per-driver bypass. A driver without a confirmed regular shift behaves exactly as today. |
| S5 | **Times are base → base (U2, §6.1)**, through one pair of helpers in `handoff_chain`:<br>• `shift_lead_min(kind, pickup_zone)` = base→zone drive (central `BASE_TO_ZONE`) + pickup buffer (10 airport / 15 other), + 25 when `kind == "evening"` (U16: report ~10 min before car-ready, plus 15-min prep).<br>• `shift_tail_min(kind, drop_zone)` = drop zone→base drive + 15 fuel when `kind == "morning"` (U14). Otherwise it is `round(car_ready_min(drop_zone)[1])`: drop → wash → fuel → base, which is 61 min after an MCO drop (U19).<br>The engine checks `pickup − lead ≥ window start` and `clear + tail ≤ window end`. Pre-fill uses the same helpers. |
| S6 | **Pre-fill (C8)** uses the 56 days before today:<br>• A pickup before 02:00 belongs to the previous day.<br>• A day's raw start is first pickup − `shift_lead_min("morning", zone)`, and its template is the one whose start band is nearest that raw start.<br>• On an Evening day the start moves a further 25 min earlier.<br>• A day's end is the latest P50 occupancy end + `shift_tail_min(kind, drop zone)`.<br>• A weekday is regular when it was worked in at least 4 of the 8 weeks.<br>• Start and end are medians. Start rounds **down** to 5 min and end rounds up to 5 min. The end is capped at start + the template's max span. |
| S7 | `extra_shift_days` is the list of weekdays the driver is open to an extra shift. Empty means not open. `hard_latest_finish` is a time plus a `hard_latest_finish_next_day` flag. |
| S8 | **Template bands are targets (U11).** A shift outside its band gets a warning. A shift longer than `max_span_minutes` (≤ 720) is an error. The seed comes from §3. The lower end of Midday's end band (15:00) and of Evening's end band (20:00) are choices made in this plan, not values from §3. |
| S9 | **Hour-only paths are unchanged in Stage 1**, and they never read availability: the farm-out optimizer's worked-span windows (`farmout_optimizer.py:792-807`) and `fleet_intel.py:374` (`configured=None`). Where an Auto-Assign modal hour was retyped by a dispatcher, the typed hours win: that driver keeps an hour window, still bypasses the stub (`"source": "regular"`), and gets no minute keys. |
| S10 | Managers (`is_superuser`) edit regular shifts, hard limits and templates. Every staff user can view them. |
| S11 | Validation checks rest both ways between consecutive working days, Sunday→Monday included, using `SchedulerSettings.rest_min_gap_minutes` (510, C7). |
| S12 | *(withdrawn in rev 2: the hour path stays byte-for-byte, wrapping windows included)* |
| S13 | Stage 1 does not change Day Setup (C3 lands in Stage 2 with weekly pairing, U23/C14). A regular car set on the profile stays Day Setup's first choice, as it is today when set in admin. Its Day Setup label changes from "his car (set in admin)" to "his regular car". |
| S14 | **Accepted Stage 1 limit:** the engine judges each date's legs against that date's window only. With the switch on, an evening driver's after-midnight work (pickups 00:00–02:59 on the next date's board) can't go to him. Stage 3 builds cross-date shifts. The smoke test reports how many legs this affects. |

## Global Constraints

- **Switch-off parity.** Every new engine behaviour sits behind `SchedulerSettings.regular_shift_windows` (default False). With the switch off, `docs/scheduling-redesign/analysis/14_pipeline_parity.py` must report `differences : 0` against `$SP/14_pipeline_parity_stage1_before.json`. The hour path of `window_check` stays byte-for-byte, wrapping windows included.
- **12h ceiling.** `ShiftTemplate.max_span_minutes` ≤ 720. A regular-shift day may not exceed its template's `max_span_minutes`. The switch-on window is never wider than `max_span_minutes`.
- **Data.** Times are stored with minute precision in **new** fields only. `DriverVehicleAssignment.planned_*` stays unwritten.
- **Drivers.** Nothing contacts a driver, and the driver app does not change (U6).
- **Permissions.** Editing requires `request.user.is_superuser`, viewing requires `request.user.is_staff`, using the existing inline-check style (`drivers/views.py:1271,1283`).
- **Switch reads.** The switch is read through `drivers.regular_shifts.regular_windows_on()`, which caches it in Django's cache under `dispatching.models.REGULAR_WINDOWS_CACHE_KEY` for 60s. Every writer deletes the key, and so does `SchedulerSettings.clear_cache()`.
- **Settings writes.** Writes to `SchedulerSettings` from the new code use `SchedulerSettings.objects.filter(pk=…).update(...)`, never a save of the process-cached row. Production runs 3 workers, each with its own cached copy.
- **No new queries for unconfirmed drivers.** The resolver reads the switch and the template cache only when `driver.regular_shift_confirmed_at` is set, and it never touches `row.shift_template`; it uses `row.shift_template_id` plus `templates_by_id()`. Several existing tests count queries.
- **Test isolation.** LocMemCache and `dispatching.models._settings_cache` both outlive the per-test DB rollback. Task 2 creates `drivers/test_support.py` with `RegularShiftCacheMixin`, whose `setUp` and `tearDown` call `SchedulerSettings.clear_cache()` and `cache.delete(SHIFT_TEMPLATES_CACHE_KEY)`. Every new test class uses it.
- **Tests.**
  - Run `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching drivers`, or one module, e.g. `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_regular_shifts`.
  - Put a "Run with:" line in each new test file's docstring.
  - For text a template autoescapes (apostrophes), assert on the form's or formset's errors, not on `assertContains`.
- **Frontend.**
  - Read `docs/claude.md` first.
  - Staff pages extend `main.html`, include `dispatching/dispatcher_navbar.html`, reuse the `drivers/_comms_admin_palette.html` tokens and the `.profile-card` pattern from `driver_profile.html`, and must work at 375px wide.
  - A `{# #}` comment must open and close on one line (`dispatching/tests_template_comments.py`).
  - Implementers do not run a browser; the controller runs one browser pass in Task 9.
- **Words on dispatcher pages.** Use "regular shift", "Morning / Midday / Evening", "never starts before", "never finishes after", "days a week", "open to extra shifts on", "regular car". Never use "template", "stub" or "window", except in the page title "Shift Templates".
- **Release note.** There is one note, `docs/release-notes/2026-10-04-regular-shifts-and-driver-facts.md`, created in Task 6.
  - Commits with no UI carry `Release-Note: none`.
  - UI commits after Task 6 update that note and carry `Release-Note: none (covered by docs/release-notes/2026-10-04-regular-shifts-and-driver-facts.md)`.
- **Commits.**
  - Subject: a plain-English outcome. Body: wrapped at about 72 columns, naming migrations as "drivers 0062". End with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Stage explicit paths only, never `git add -A` or `git add .`, and commit straight after staging.
  - Never commit anything in `docs/scheduling-redesign/analysis/out/14_pipeline_parity_stage1_*.json` (move those files to `$SP`), and never commit `content/db.sqlite3`.

## Review Focus

1. **The legacy editors must not wipe a regular shift.** Saving the Edit Schedules drawer, or the planner's Driver Schedules modal, for a driver who has a confirmed regular shift must leave `shift_template`, `shift_start` and `shift_end` untouched. Test in Task 3.
2. **Confirming must not change what the engine sees while the switch is off.** Confirming a regular shift for a driver who had missing weekday rows leaves every legacy key `resolve_effective_availability` returns unchanged, for all 7 days. Test in Task 3.
3. **A driver without a regular shift after the switch is on.** A driver added or reactivated after the switch is on shows on the list, the switch stays on, and their windows behave exactly as today. Tests in Tasks 4 and 7.
4. **No recent trips.** A driver with no trips in 8 weeks is suggested Off for every day, the page says "no trips in the last 8 weeks", and the shift can still be confirmed. Tests in Tasks 3 and 7.
5. **Hard limits edited later.** A profile edit that puts a confirmed regular day outside the hard limits is refused, and the error names that day. Test in Task 6.

---

## File map

| File | Responsibility |
|---|---|
| `dispatching/feasibility_guards.py` | Minute path in `window_check` (lead/tail); regular windows bypass the stub; `regular_window_keys(eff)`; `legacy_hours()` |
| `dispatching/handoff_chain.py` | `base_drive_min`, `shift_lead_min`, `shift_tail_min`, `HANDOVER_FUEL_MIN`, `EVENING_REPORT_LEAD_MIN` |
| `drivers/models.py` + `drivers/migrations/0062_shift_facts.py`, `0063_seed_shift_templates.py` | `ShiftTemplate`; `Driver` facts; regular-shift fields on `DriverWeeklySchedule`; `SHIFT_TEMPLATES_CACHE_KEY` |
| `dispatching/models.py` + `dispatching/migrations/0022_schedulersettings_regular_shift_windows.py` | The switch, `GUARDED_FIELDS`, `REGULAR_WINDOWS_CACHE_KEY` |
| `drivers/test_support.py` (new) | `RegularShiftCacheMixin` |
| `drivers/regular_shifts.py` (new) | Switch read/write, roster, suggestions, validation, save, labels |
| `drivers/availability.py` | Regular-shift keys; the window when the switch is on; labels; minute-aware `is_pickup_within_window` |
| `dispatching/{assignment_pipeline,scheduler,swap_optimizer,board_validation,conflict_advisor,conflict_advisor_actions,farmout_actions,day_planner,views}.py` | Pass the regular keys to every window builder |
| `drivers/forms.py`, `drivers/regular_shift_views.py` (new), `drivers/urls.py`, templates | UI |

---

### Task 1: Minute, cross-midnight and base→base windows in the rules door

**Files:**
- Modify: `dispatching/feasibility_guards.py`
  - `window_check` (:360-432)
  - `get_effective_window` (:291-357)
  - new helpers next to them
- Modify: `dispatching/handoff_chain.py` (new helpers after `car_ready_min`)
- Modify: `dispatching/scheduler.py`. Pass categories to the only two `window_check` callers:
  - `check_feasibility` :1238 passes `pickup_category=new_pickup_cat, dropoff_category=new_dropoff_cat`, which are already computed at :1209-1210.
  - `_chain_ok` :2579 passes `pickup_category=s.pickup_category, dropoff_category=s.dropoff_category`.
- Test: `dispatching/tests_minute_windows.py` (new, `SimpleTestCase`)

**Interfaces (produced):**
- **Window dict.** A window dict may carry:
  - `start_min: int`, `end_min: int`: minutes after 00:00 of `target_date`, with `start_min < end_min ≤ start_min + 1440`. A value over 1440 means the next day.
  - `kind: str`: `"morning" | "midday" | "evening"`.
  - `source: "regular"`.

  Hour windows carry none of these keys.
- **`window_check(...)`.** The signature gains `pickup_category=None, dropoff_category=None` (keyword, last).
  - When the window has `start_min`, the minute path runs:
    - pickup minutes `p = h*60+m`
    - `lead = shift_lead_min(kind, pickup_category)` and `tail = shift_tail_min(kind, dropoff_category)` when both category and `kind` are given, else 0
    - **start:** reject when `p - lead < start_min`
    - **night rule:** same as the hour path, but the escape is `start_min <= p`
    - **CLEAR_BY:** reject when `clear_dt + tail > base_dt + end_min`, where `base_dt = datetime.combine(base_date, time(0))`, and `base_date` is `target_date`, or, when that is None, `clear_dt.date()` if `clear_dt.time() >= pickup_time` else `clear_dt.date() − 1 day`
    - **LAST_PICKUP:** reject when `p > end_min`
    - flexible semantics exactly as in the hour path (start bypassed; clear-by only if `FLEXIBLE_RESPECTS_CLEAR_BY`)
    - the max-hours block is unchanged
  - The hour path is unchanged, byte for byte.
  - Reason strings (pinned by tests). `HH:MM` uses `%H:%M`, and the suffix ` (next day)` is added when `end_min >= 1440`:
    - lead 0: `"pickup HH:MM before start HH:MM"`
    - lead > 0: `"pickup HH:MM means leaving base HH:MM, before start HH:MM"`
    - tail 0: `"clears HH:MM after clear-by HH:MM"`
    - tail > 0: `"clears HH:MM, back at base HH:MM, after HH:MM"`
    - last pickup: `"pickup HH:MM after last-pickup HH:MM"`
- **`get_effective_window(driver_id, configured=None, enforce_cap=True)`.** Same signature. When `configured` has `source == "regular"`, it skips the stub branch and goes through the non-stub logic (cap when `enforce_cap`, `night_exempt = not enforce_cap`), keeping every key.
- **`regular_window_keys(eff: dict) -> dict`.** Returns `{"start_min", "end_min", "kind", "source": "regular"}` built from `eff["window_start_min"]`, `eff["window_end_min"]` and `eff["window_kind"]` when start and end are both non-None, otherwise `{}`. It uses `.get`, so a missing key counts as None.
- **`legacy_hours(start_min: int, end_min: int) -> tuple[int, int]`** returns `(start_min // 60, 23 if end_min >= 1440 else min(23, ceil(end_min / 60)))`.
- **`handoff_chain` additions:**
  - `HANDOVER_FUEL_MIN = 15`
  - `EVENING_REPORT_LEAD_MIN = 25`
  - `base_drive_min(zone) -> int`, the central `_base_to(zone)[1]`
  - `shift_lead_min(kind, pickup_zone) -> int`
  - `shift_tail_min(kind, drop_zone) -> int`, per S5

  Check them: Morning from MCO is lead 22 and tail 27; Evening into MCO is tail 61; Evening lead at Disney is 35 + 15 + 25 = 75. Comment that §6.2 names these values as Stage 3 settings.

- [ ] **Step 1: Write the failing tests.**
  - Copy today's `window_check` into the test module as `_legacy_window_check`, verbatim. Import what it needs: `from datetime import datetime, time as dt_time` and `from dispatching.feasibility_guards import END_HOUR_MODE, FLEXIBLE_RESPECTS_CLEAR_BY, NIGHT_LEG_FLEX_BLOCK, NIGHT_LEG_BOUNDARY_HOUR`.
  - Helpers:
    - `W(start=None, end=None, **extra)` builds `{"start", "end", "max_hours": None, "flexible": False, **extra}`
    - `D = date(2026, 10, 7)`
    - `dt(h, m, day=0)` returns `datetime.combine(D + timedelta(days=day), time(h, m))`

```python
def test_hour_windows_match_legacy_on_grid(self):
    # EVERY hour window: start in (None, 0..23) x end in (None, 0..23) — wrapping ones included
    # (an approved day off makes (stub_start, 0)); pickups 00:00-23:45 step 15; clears +0..+300
    # step 30; modes CLEAR_BY/LAST_PICKUP; flexible True/False; target_date D and None.
    self.assertEqual(fg.window_check(w, p, c, 5.0, target_date=td, mode=m),
                     _legacy_window_check(w, p, c, 5.0, target_date=td, mode=m))

def test_hour_window_wrap_unchanged(self):
    self.assertEqual(fg.window_check(W(start=6, end=0), time(22, 0), dt(23, 0), 1, target_date=D),
                     (False, "clears 23:00 after clear-by 0:00"))

def test_minute_start_without_categories(self):
    w = W(start=4, end=17, start_min=250, end_min=970, kind="morning", source="regular")
    self.assertEqual(fg.window_check(w, time(4, 5), dt(5, 0), 1, target_date=D),
                     (False, "pickup 04:05 before start 04:10"))
    self.assertTrue(fg.window_check(w, time(4, 10), dt(5, 0), 1, target_date=D)[0])

def test_minute_lead_uses_pickup_zone(self):
    w = W(start=4, end=17, start_min=275, end_min=995, kind="morning", source="regular")  # 04:35
    self.assertTrue(fg.window_check(w, time(5, 0), dt(6, 0), 1, target_date=D,
                                    pickup_category="MCO Terminal", dropoff_category="Disney Resort")[0])
    self.assertEqual(fg.window_check(w, time(5, 0), dt(6, 0), 1, target_date=D,
                                     pickup_category="Disney Resort", dropoff_category="MCO Terminal"),
                     (False, "pickup 05:00 means leaving base 04:10, before start 04:35"))

def test_minute_tail_morning_fuel(self):
    w = W(start=4, end=17, start_min=275, end_min=995, kind="morning", source="regular")  # ends 16:35
    self.assertTrue(fg.window_check(w, time(15, 0), dt(16, 8), 1, target_date=D,
                                    pickup_category="Disney Resort", dropoff_category="MCO Terminal")[0])
    self.assertEqual(fg.window_check(w, time(15, 0), dt(16, 9), 1, target_date=D,
                                     pickup_category="Disney Resort", dropoff_category="MCO Terminal"),
                     (False, "clears 16:09, back at base 16:36, after 16:35"))

def test_cross_midnight_evening_tail(self):
    w = W(start=14, end=23, start_min=855, end_min=1575, kind="evening", source="regular")  # ends 02:15
    self.assertTrue(fg.window_check(w, time(23, 30), dt(1, 14, day=1), 1, target_date=D,
                                    pickup_category="MCO Terminal", dropoff_category="MCO Terminal")[0])
    self.assertEqual(fg.window_check(w, time(23, 30), dt(1, 15, day=1), 1, target_date=D,
                                     pickup_category="MCO Terminal", dropoff_category="MCO Terminal"),
                     (False, "clears 01:15, back at base 02:16, after 02:15 (next day)"))

def test_cross_midnight_without_target_date(self):
    w = W(start=14, end=23, start_min=855, end_min=1575, kind="evening", source="regular")
    self.assertTrue(fg.window_check(w, time(22, 30), dt(0, 45, day=1), 1)[0])
    self.assertFalse(fg.window_check(w, time(23, 50), dt(2, 16, day=1), 1)[0])

def test_minute_last_pickup_mode(self):
    w = W(start=4, end=16, start_min=250, end_min=930, kind="midday", source="regular")
    self.assertTrue(fg.window_check(w, time(15, 30), dt(16, 0), 1, target_date=D, mode="LAST_PICKUP")[0])
    self.assertEqual(fg.window_check(w, time(15, 31), dt(16, 0), 1, target_date=D, mode="LAST_PICKUP"),
                     (False, "pickup 15:31 after last-pickup 15:30"))

def test_minute_flexible_semantics_match_hours(self):
    w = W(start=1, end=10, start_min=90, end_min=600, kind="midday", source="regular", flexible=True)
    self.assertTrue(fg.window_check(w, time(1, 30), dt(11, 0), 1, target_date=D)[0])   # escape + no clear-by
    self.assertFalse(fg.window_check(w, time(1, 15), dt(2, 0), 1, target_date=D)[0])   # night rule

def test_regular_window_bypasses_stub(self):              # id 46 is stubbed 6-20, 14h
    cfg = {"start": 4, "end": 17, "start_min": 250, "end_min": 970, "kind": "morning",
           "source": "regular", "max_hours": None, "flexible": False}
    w = fg.get_effective_window(46, configured=cfg)
    self.assertEqual((w["start"], w["end"], w["start_min"], w["end_min"]), (4, 17, 250, 970))
    self.assertEqual(w["max_hours"], fg.SPAN_HARD_HOURS_DEFAULT); self.assertFalse(w["night_exempt"])

def test_regular_hour_window_bypasses_stub(self):         # retyped modal hours (S9)
    w = fg.get_effective_window(46, configured={"start": 4, "end": 22, "max_hours": None,
                                                "flexible": False, "source": "regular"})
    self.assertEqual((w["start"], w["end"]), (4, 22)); self.assertNotIn("start_min", w)

def test_regular_window_manual_sovereign(self):
    w = fg.get_effective_window(46, configured={"start": 4, "end": 17, "start_min": 250,
        "end_min": 970, "kind": "morning", "source": "regular", "max_hours": None,
        "flexible": False}, enforce_cap=False)
    self.assertTrue(w["night_exempt"]); self.assertIsNone(w["max_hours"])

def test_regular_window_keys(self):
    self.assertEqual(fg.regular_window_keys({}), {})
    self.assertEqual(fg.regular_window_keys({"window_start_min": None, "window_end_min": 930}), {})
    self.assertEqual(fg.regular_window_keys({"window_start_min": 250, "window_end_min": 970,
                                             "window_kind": "morning"}),
                     {"start_min": 250, "end_min": 970, "kind": "morning", "source": "regular"})

def test_legacy_hours(self):
    self.assertEqual(fg.legacy_hours(250, 970), (4, 17))
    self.assertEqual(fg.legacy_hours(855, 1575), (14, 23))
    self.assertEqual(fg.legacy_hours(420, 1140), (7, 19))

def test_shift_lead_tail(self):
    self.assertEqual(hc.shift_lead_min("morning", "MCO Terminal"), 22)
    self.assertEqual(hc.shift_tail_min("morning", "MCO Terminal"), 27)
    self.assertEqual(hc.shift_tail_min("evening", "MCO Terminal"), 61)
    self.assertEqual(hc.shift_lead_min("evening", "Disney Resort"), 75)
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_minute_windows`. Expected: only the grid and wrap-unchanged tests pass.
- [ ] **Step 3: Implement the changes listed under Interfaces.** Put the minute path in a private `_minute_window_check(...)` that `window_check` calls first when `start_min` is present. Leave the hour path's lines untouched. Update the module docstring about `USE_STUB_WINDOWS` to describe the per-driver bypass.
- [ ] **Step 4: Run the tests and confirm they pass.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_minute_windows dispatching.tests_feasibility_guards dispatching.tests_span_caps dispatching.tests_founder_brain`. Expected: all pass.
- [ ] **Step 5: Commit** `dispatching/feasibility_guards.py`, `dispatching/handoff_chain.py`, `dispatching/scheduler.py` and the test. Subject: "Driver windows can be set to the minute, run past midnight and count the drive from base". End the body with `Release-Note: none`.

---

### Task 2: Driver facts, shift templates and the switch (models)

**Files:**
- Modify: `drivers/models.py`
  - Add `SHIFT_TEMPLATES_CACHE_KEY = "drivers:shift_templates"` at module level.
  - Add `ShiftTemplate` above `DriverWeeklySchedule`.
  - Add the new fields to `Driver` after `night_bonus`, and to `DriverWeeklySchedule` after `scheduling_notes`.
- Create: `drivers/migrations/0062_shift_facts.py` (schema) and `drivers/migrations/0063_seed_shift_templates.py` (data: historical models, reversible).
- Modify: `dispatching/models.py`
  - Add the module constant `REGULAR_WINDOWS_CACHE_KEY = "scheduler:regular_shift_windows"`.
  - Add the field, plus `GUARDED_FIELDS = frozenset({"regular_shift_windows"})`.
  - `clear_cache()` also deletes `REGULAR_WINDOWS_CACHE_KEY` from Django's cache.
  - `reset_to_defaults()` skips `GUARDED_FIELDS` and saves with `update_fields` set to the fields it reset.
- Create: `dispatching/migrations/0022_schedulersettings_regular_shift_windows.py`
- Modify: `dispatching/views.py` `update_scheduler_settings` (:17182). It skips names in `GUARDED_FIELDS` and saves with `settings.save(update_fields=updated)`.
- Create: `drivers/test_support.py` (`RegularShiftCacheMixin`; see Global Constraints)
- Test: `drivers/tests_shift_facts.py` (new)

**Interfaces (produced):**
- **`ShiftTemplate`**
  - `KIND_CHOICES = [("morning","Morning"),("midday","Midday"),("evening","Evening")]`
  - Fields:
    - `kind`: CharField(12), unique
    - `name`: CharField(40)
    - `start_earliest`, `start_latest`, `end_earliest`, `end_latest`: TimeField
    - `max_span_minutes`: PositiveSmallIntegerField, default 720, validators `MinValueValidator(60)` and `MaxValueValidator(720)`
    - `notes`: CharField(300), blank
    - `sort_order`: PositiveSmallIntegerField, default 0
    - `updated_at`: auto_now
    - `updated_by`: FK User, null, **blank**, SET_NULL, `related_name="+"`
  - `Meta.ordering = ["sort_order"]`
  - `clean()` raises `ValidationError({"start_latest": …})` when `start_earliest > start_latest`.
  - `start_band_minutes()` and `end_band_minutes()` return `tuple[int, int]`. Add 1440 to `end_latest` when it is earlier than `end_earliest`.
  - `band_label() -> str`, using `drivers.availability.fmt_time_long`, e.g. `"leaves 3 AM–6 AM, back 12 PM–4 PM"`.
  - `__str__` returns `name`.
- **`DriverWeeklySchedule`** gets `shift_template` (FK ShiftTemplate, null, blank, PROTECT, `related_name="weekly_rows"`), `shift_start` and `shift_end` (TimeField, null, blank). `regular_minutes() -> Optional[tuple[int, int]]` adds 1440 to the end when `shift_end <= shift_start`. It returns None when `shift_template_id is None` or either time is None; it never touches `self.shift_template`.
- **`Driver`** gets:
  - `hard_earliest_start` and `hard_latest_finish`: TimeField, null, blank
  - `hard_latest_finish_next_day`: Bool, default False
  - `max_days_per_week`: PositiveSmallIntegerField, null, blank, validators 1–7
  - `extra_shift_days`: JSONField, `default=list`, blank
  - `regular_shift_confirmed_at`: DateTimeField, null, blank
  - `regular_shift_confirmed_by`: FK User, null, blank, SET_NULL, `related_name="+"`
  - `hard_window_minutes() -> tuple[Optional[int], Optional[int]]`, which adds 1440 to latest when the next-day flag is set
  - a `has_regular_shift` property

  Every new field gets plain-English `help_text`.
- **`SchedulerSettings.regular_shift_windows`**: `BooleanField(default=False, help_text="Auto-assign reads each driver's hours from their confirmed regular shift instead of the old fixed table. Turned on from the Regular Shifts page once every driver has one.")`
- **Seed (0063)**, one note per row taken from §3:

| kind | name | start band | end band | max span | sort |
|---|---|---|---|---|---|
| morning | Morning | 03:00–06:00 | 12:00–16:00 | 720 | 1 |
| midday | Midday | 06:00–09:00 | 15:00–21:00 | 720 | 2 |
| evening | Evening | 12:00–16:00 | 20:00–02:15 | 720 | 3 |

- [ ] **Step 1: Write the failing tests** (`drivers/tests_shift_facts.py`, using `RegularShiftCacheMixin`):

```python
def test_seeded_templates(self):
    self.assertEqual(list(ShiftTemplate.objects.values_list("kind", "start_earliest", "start_latest",
                                                            "end_earliest", "end_latest", "max_span_minutes")),
        [("morning", time(3), time(6), time(12), time(16), 720),
         ("midday", time(6), time(9), time(15), time(21), 720),
         ("evening", time(12), time(16), time(20), time(2, 15), 720)])

def test_template_span_ceiling(self):
    t = ShiftTemplate.objects.get(kind="morning"); t.max_span_minutes = 721
    with self.assertRaises(ValidationError) as cm: t.full_clean()
    self.assertIn("max_span_minutes", cm.exception.message_dict)

def test_evening_end_band_crosses_midnight(self):
    self.assertEqual(ShiftTemplate.objects.get(kind="evening").end_band_minutes(), (1200, 1575))

def test_weekly_regular_minutes(self):     # evening 14:15-02:15 -> (855, 1575); 04:10-15:30 -> (250, 930); no template -> None
def test_hard_window_minutes(self):        # 05:00 / 01:00 + next_day -> (300, 1500); 05:00 / 23:00 -> (300, 1380); blank -> (None, None)
def test_new_driver_defaults(self):        # extra_shift_days == [], has_regular_shift False

def test_generic_settings_endpoint_cannot_flip_switch(self):
    resp = self.client.post(reverse("update_scheduler_settings"),
        data=json.dumps({"regular_shift_windows": 1, "min_turn_buffer": 7}), content_type="application/json")
    self.assertEqual(resp.status_code, 200); self.assertNotIn("regular_shift_windows", resp.json()["updated"])
    self.assertFalse(SchedulerSettings.objects.get(pk=1).regular_shift_windows)

def test_stale_cached_row_cannot_write_switch_back(self):
    SchedulerSettings.get_settings()                                   # cache the row (switch False)
    SchedulerSettings.objects.filter(pk=1).update(regular_shift_windows=True)
    self.client.post(reverse("update_scheduler_settings"), data=json.dumps({"min_turn_buffer": 7}),
                     content_type="application/json")
    self.assertTrue(SchedulerSettings.objects.get(pk=1).regular_shift_windows)

def test_reset_keeps_switch(self):         # switch True in DB, POST {"reset": true} -> still True
def test_clear_cache_drops_switch_key(self):
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_shift_facts`.
- [ ] **Step 3: Implement.** Generate the schema migrations with `python manage.py makemigrations drivers dispatching` and rename them. Hand-write 0063.
- [ ] **Step 4: Run the tests and confirm they pass.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_shift_facts dispatching.tests_feasibility_guards drivers.tests`, then `python manage.py makemigrations --check --dry-run`. Expected: tests pass, then "No changes detected".
- [ ] **Step 5: Commit** the **six** source files, `drivers/test_support.py` and the test. Subject: "Drivers carry hard limits and a regular-shift slot; Morning, Midday and Evening shapes are seeded". End the body with `Release-Note: none`.

---

### Task 3: The regular-shift module

**Files:**
- Create: `drivers/regular_shifts.py`
- Test: `drivers/tests_regular_shifts.py` (new)

**Interfaces:**
- **Consumes:**
  - Task 1: `fg.legacy_hours`, `hc.shift_lead_min`, `hc.shift_tail_min`, `hc.occupancy_kind`, `hc.occupancy_interval`
  - Task 2: the models, `REGULAR_WINDOWS_CACHE_KEY`, `SHIFT_TEMPLATES_CACHE_KEY`
  - Existing: `drivers.availability.fmt_time_long`, `dispatching.day_setup._is_excluded`, `dispatching.analytics.categorize_location`
- **Produces:**

```python
LOOKBACK_DAYS = 56; REGULAR_DAY_MIN_WEEKS = 4; ROUND_MIN = 5; NIGHT_TAIL_END = time(2, 0)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

@dataclass(frozen=True)
class DayShift:
    day: int; template_id: Optional[int]; start: Optional[time]; end: Optional[time]
    def minutes(self) -> Optional[tuple[int, int]]: ...

@dataclass(frozen=True)
class DaySuggestion:
    day: int; weeks_worked: int; template_id: Optional[int]; start: Optional[time]; end: Optional[time]

def regular_windows_on() -> bool
def set_regular_windows(on: bool, user) -> tuple[bool, str]
def templates_by_id() -> dict[int, "ShiftTemplate"]
def clear_template_cache() -> None
def roster_drivers() -> list["Driver"]                # active, inhouse, portal_role="driver", not _is_excluded; by name
def drivers_without_regular_shift() -> list["Driver"]
def current_days(driver) -> list[DayShift]            # 7 items from the weekly rows' new fields
def suggest_regular_shifts(drivers, today: date) -> dict[int, list[DaySuggestion]]
def validate_regular_shift(days, *, templates, hard_earliest_start, hard_latest_finish,
                           hard_latest_finish_next_day, max_days_per_week, rest_min) -> list[str]
def limit_messages(days, *, hard_earliest_start, hard_latest_finish,
                   hard_latest_finish_next_day, max_days_per_week) -> list[str]   # the 3 limit checks only
def band_warnings(days, templates) -> list[str]
def band_fill(template) -> tuple[time, time]          # default times when a shape is picked with blank times
def save_regular_shift(driver, days, user) -> None    # atomic; raises ValueError if validate_* returns anything
def summary_label(days, templates) -> str
def day_label(day: DayShift, templates) -> str        # "Morning 4:10 AM – 3:30 PM" / "Off"
```

- **`regular_windows_on`** uses `cache.get(REGULAR_WINDOWS_CACHE_KEY)`. On a miss it reads `SchedulerSettings.objects.filter(pk=1).values_list("regular_shift_windows", flat=True).first()` (a missing row counts as False) and caches the result for 60s.
- **`set_regular_windows`.** Turning on while the list is not empty returns `(False, "{n} drivers still need a regular shift: {first 3 names}…")`, with no ellipsis when n ≤ 3 and "1 driver still needs" when n = 1. Otherwise it gets or creates the row, runs `filter(pk=…).update(regular_shift_windows=on)`, calls `SchedulerSettings.clear_cache()`, writes an INFO log naming the user, and returns `(True, "")`. Turning off always succeeds.
- **`templates_by_id`** caches `{id: template}` under `SHIFT_TEMPLATES_CACHE_KEY` for 60s. `clear_template_cache()` deletes that key.
- **`suggest_regular_shifts`** runs ONE `Leg` query over `[today − 56d, today)` for every requested driver. It excludes `status="cancelled"` and reservation status in `("cancelled","canceled")`, and does not filter on completed. It applies S6 and builds zones with `categorize_location`. A weekday worked fewer than 4 times gives `template_id=None, start=end=None`.
- **`validate_regular_shift`** returns messages in day order, using `fmt_time_long`:
  - `"{Day}: pick a start and an end time, or set the day to Off."`
  - `"{Day}: a shift longer than {h} hours isn't allowed."`, where h is `max_span_minutes/60` printed as `12` or `11.5`.
  - `"{Day}: starts at {t} — before this driver's earliest start ({t})."`
  - `"{Day}: ends at {t} — after this driver's latest finish ({t})."`
  - `"{n} working days — more than this driver's limit of {m} a week."`
  - `"{Day} to {Next}: only {H}h {M}m off between shifts; the minimum is {RH}h {RM}m."`
- **`limit_messages`** returns only the earliest-start, latest-finish and days-a-week messages. The profile form uses it.
- **`band_warnings`** produces `"{Day}: {start}–{end} is outside the usual {Name} shape ({band_label})."`
- **`band_fill(t)`** returns `(start_latest, min(end_latest, start_latest + max_span))`, the end band crossing midnight where needed. For the seed this gives Morning 06:00–16:00, Midday 09:00–21:00 and Evening 16:00–02:15.
- **`save_regular_shift`**:
  - Validates first and raises `ValueError` on any message.
  - Uses `update_or_create`. Its defaults set **only** `shift_template_id`, `shift_start` and `shift_end`.
  - A newly created row copies the driver's `default_*` values (S1): `is_available=True`, `shift_type=default_shift_type`, `start_hour`, `end_hour`, `flexible`, `max_hours`, `preferred_shift`, `preference`.
  - Finally sets `regular_shift_confirmed_at=timezone.now()` and `regular_shift_confirmed_by=user`.
- **`summary_label`** groups consecutive days with the same shift, e.g. `"Mon–Fri Morning 4:10 AM–3:30 PM · Sat Evening 2:15 PM–2:15 AM"`. With no working days it returns `"Off every day"`.

- [ ] **Step 1: Write the failing tests.** Use `RegularShiftCacheMixin`. Build Leg fixtures the way `dispatching/tests_fleet_day.py` does. Take location strings that `categorize_location` maps to "MCO Terminal" and "Disney Resort" from that module's keyword lists. Use `today = date(2026, 10, 5)` (a Monday).
  - `test_suggest_regular_weekday`: 5 of 8 Mondays have a first pickup at 05:00 at MCO. Monday is Morning, `start == time(4, 35)`, `weeks_worked == 5`.
  - `test_suggest_irregular_weekday_is_off`: 3 of 8 Tuesdays means `template_id is None`.
  - `test_suggest_evening_start_includes_report_and_end_includes_night_return`: first pickups around 15:00 at MCO, last pickup 23:00 MCO→MCO. Start = raw − 25 (rounded down). End = P50 occupancy end + 61 (rounded up).
  - `test_suggest_night_tail_counts_for_previous_day`
  - `test_suggest_end_capped_at_template_span`
  - `test_suggest_no_recent_trips` (Review Focus 4)
  - `test_suggest_one_query`: after `clear_template_cache()`, `assertNumQueries(2)` for 5 drivers.
  - `test_validate_messages`: each message as a literal, including `"Sunday to Monday: only 6h 0m off between shifts; the minimum is 8h 30m."`
  - `test_band_fill_is_valid_for_every_seeded_shape`: `validate_regular_shift` returns `[]` for each shape's fill.
  - `test_band_warning`
  - `test_save_refuses_invalid`: a 13h day raises `ValueError` and writes nothing.
  - `test_save_creates_rows_with_legacy_defaults_parity` (Review Focus 2): a driver with no weekly rows, `default_start_hour=5`, `default_flexible=False`. The legacy keys of `resolve_effective_availability` are identical before and after for all 7 days: `is_available, shift_type, start_hour, end_hour, flexible, max_hours, preferred_shift, preference, status, display_label, tooltip`.
  - `test_legacy_save_keeps_regular_fields` (Review Focus 1): POST `save_driver_weekly_schedules` with the planner-modal payload shape → the regular fields are unchanged.
  - `test_switch_refused_until_list_empty` (message pinned), `test_switch_on_when_all_confirmed` (all Off counts), `test_switch_off_always_allowed`, `test_switch_cache_cleared_on_set`
  - `test_roster_excludes_placeholder_and_affiliates`
  - `test_summary_label`, `test_day_label`
- [ ] **Step 2: Run the tests and confirm they fail.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_regular_shifts`.
- [ ] **Step 3: Implement `drivers/regular_shifts.py`.**
- [ ] **Step 4: Run the tests and confirm they pass.** Same command.
- [ ] **Step 5: Commit.** Subject: "Regular shifts can be suggested from the last 8 weeks, checked and saved". End the body with `Release-Note: none`.

---

### Task 4: The availability door applies a confirmed regular shift (switch on)

**Files:**
- Modify: `drivers/availability.py`
- Test: `drivers/tests_availability_regular.py` (new)

**Interfaces:**
- **Consumes:** Task 3's `regular_windows_on` and `templates_by_id`; Task 1's `fg.legacy_hours`; Task 2's `regular_minutes` and `hard_window_minutes`.
- **Produces:** `resolve_effective_availability(driver, target_date, *, regular_windows=None)`. With `None`, the resolver calls `regular_windows_on()`, but only when `driver.regular_shift_confirmed_at` is set. Every result gains:
  - `regular_shift`: None, or `{"kind", "name", "start_min", "end_min", "label"}` for the target weekday's confirmed working day, with `label` from `regular_shifts.day_label`.
  - `regular_day_off`: True when the driver is confirmed and this weekday is Off.
  - `hard_earliest_start`, `hard_latest_finish` and `hard_latest_finish_next_day`.
  - `window_start_min`, `window_end_min` and `window_kind`. These are set **only** when the switch is on.
- **Switch on, confirmed driver, working day.** Before exceptions are applied, the base layer becomes:
  - `is_available=True`, `shift_type=kind`, `flexible=False`
  - the window from S4, worked from the template's `max_span_minutes`:
    - morning `(s, s+M)`
    - evening `(max(0, e−M), e)`
    - midday `(s, e)`
  - clipped by `hard_window_minutes()`
  - `(start_hour, end_hour) = fg.legacy_hours(window)`
- **Switch on, confirmed driver, Off day:** `is_available=False`.
- **Exceptions.** The existing exception rules then run unchanged. In addition:
  - `off` clears the window keys.
  - `flexible` clears the window keys.
  - Partial exceptions keep them.
- **Labels.**
  - `_underlying_label` returns `regular_shift["label"]` when the window keys are set.
  - The `fixed_window` tooltip reads `"Regular {Name} shift, {start} – {end}."`, adding `" (ends next day)"` when the shift crosses midnight.
- **`is_pickup_within_window`.** When `window_start_min` is set, a pickup is outside when `p < start_min or p >= end_min`. The message is `"Pickup at {t} is outside the driver's regular shift ({start}–{end})."`, with times from the window.

- [ ] **Step 1: Write the failing tests.** Use `RegularShiftCacheMixin` and the switch via `set_regular_windows`, or `regular_windows=True`.
  - `test_switch_off_new_keys_only`: confirmed driver. Legacy keys equal those of an unconfirmed twin, the window keys are None, and `regular_shift["label"] == "Morning 4:10 AM – 3:30 PM"`.
  - `test_switch_on_morning_envelope`: 04:10–15:30 gives `(window_start_min, window_end_min) == (250, 970)`, `(start_hour, end_hour) == (4, 17)`, `flexible is False`, `status == "fixed_window"`, `display_label == "Morning 4:10 AM – 3:30 PM"`.
  - `test_switch_on_evening_envelope_crosses_midnight`: 14:15–02:15 gives `(855, 1575)`, `end_hour == 23`, label `"Evening 2:15 PM – 2:15 AM"`.
  - `test_switch_on_midday_keeps_both_edges`: 07:00–19:00 gives `(420, 1140)`.
  - `test_switch_on_off_day`
  - `test_switch_on_time_off_wins`
  - `test_switch_on_flexible_exception_clears_window`
  - `test_switch_on_partial_exception_keeps_window`
  - `test_switch_on_unconfirmed_driver_unchanged` (Review Focus 3)
  - `test_hard_limits_clip_window`: hard earliest 04:30 gives `window_start_min == 270`.
  - `test_pickup_within_regular_window`
  - `test_no_extra_queries_unconfirmed`: `assertNumQueries(0)` with a cold cache, with weekly rows and overrides prefetched.
  - `test_no_extra_queries_confirmed_warm`: `assertNumQueries(0)` once the caches are warm.
- [ ] **Step 2: Run the tests and confirm they fail.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_availability_regular`.
- [ ] **Step 3: Implement.** Keep `_weekly_or_defaults` unchanged. Add `_apply_regular(base, driver, entry, templates)`.
- [ ] **Step 4: Run the tests and confirm they pass.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_availability_regular drivers.tests drivers.tests_regular_shifts`.
- [ ] **Step 5: Commit.** Subject: "With the switch on, a confirmed regular shift is the driver's hours for the day". End the body with `Release-Note: none`.

---

### Task 5: Engine paths carry the regular window, proven identical with the switch off

Each site below only merges the regular keys. With the switch off those keys are always `{}`, so nothing changes.

**Files:**
- **`dispatching/assignment_pipeline.py`**
  - Add `PipelineWindows.regular_keys: Dict[int, dict] = field(default_factory=dict)`, documented as `{driver_id: dict merged into that driver's configured window}`.
  - Merge it into the configured dict at :219.
  - Pass `regular_keys=windows.regular_keys or None` to the greedy call (:341) and to `compact_gaps_via_relocation` (:457).
  - At build-first (:280), pass `regular_keys=windows.regular_keys.get(did)` to `build_smart_schedule`.
- **`dispatching/scheduler.py`**
  - `suggest_assignments_clustered` (:1674) and `suggest_assignments` (:1739) take `regular_keys: Dict[int, dict] = None`, passed through at :1705 and :1728.
  - `_configured_window` (:1776) merges `regular_keys.get(did, {})`. When `did` is not in `driver_hours` but `regular_keys[did]` has `start_min`, it builds `{"start","end"} = fg.legacy_hours(...)`, `max_hours=None` and `flexible=False`, plus the keys.
  - `evict_to_farm_for_value` (:2660-2667), `trim_spans_via_relocation` (:3101-3104) and `compact_gaps_via_relocation` (:3289-3292) take `regular_keys=None`. When it is given, they merge `regular_keys.get(d.id, {})`. Otherwise they merge `fg.regular_window_keys(eff)`.
  - `build_smart_schedule` (:3474) takes `regular_keys: dict = None` and merges it into `_dwindow` (:3535).
- **Merge `**fg.regular_window_keys(eff)` at:**
  - `dispatching/swap_optimizer.py:222`
  - `board_validation.py:583-592`
  - `conflict_advisor_actions.py:447-450`
  - `farmout_actions.py:399-406`
  - `views.py:4356` (`check_driver_feasibility`)
  - `conflict_advisor.py:425-434`, which also tags `"stub"` only when there are no regular keys
- **`dispatching/views.py` `auto_assign_drivers` (:14674-14720).** Build `regular_keys` from each working driver's eff (`d.get_effective_availability(target_date)`):
  - In the modal path, a driver whose eff has window keys gets the full `fg.regular_window_keys(eff)` when the payload is not flexible and `(sh, eh) == (eff["start_hour"], eff["end_hour"])`. Otherwise (typed hours win, S9) they get `{"source": "regular"}`.
  - In the fallback path, use the full keys.
  - Pass the result through `PipelineWindows(regular_keys=…)` (:14754).
- **`views.py:13529` (capacity planner) and `:19189` (swap tester).** Pass `regular_keys={d.id: fg.regular_window_keys(eff) …}` to `suggest_assignments_clustered`, for in-house drivers whose eff has keys.
- **`dispatching/day_planner.py`**
  - `_day_roster` returns a 5th value, `regular_keys`, using the fallback rule. Its caller at :455 stores it as `ctx["regular_keys"]`, and :258 passes `regular_keys=ctx.get("regular_keys") or {}`.
  - Pass C bench (:782-787) copies `ctx2["regular_keys"] = dict(ctx.get("regular_keys") or {})` and adds `fg.regular_window_keys(fa)` for the bench driver. :826 also saves `ctx["regular_keys"]`.
- **Unchanged:** `farmout_optimizer.py` and `fleet_intel.py` (S9).
- **Test:** `dispatching/tests_regular_shift_engine.py` (new; uses `RegularShiftCacheMixin`)

**Interfaces:**
- **Consumes:** `fg.regular_window_keys` and `fg.legacy_hours` (Task 1), the eff keys (Task 4), `set_regular_windows` and `save_regular_shift` (Task 3).
- **Produces:** `PipelineWindows.regular_keys`, plus the `regular_keys` keyword on every function listed above.

- [ ] **Step 1: Write the failing tests.** They go through `auto_assign_drivers` in preview mode, posted the way `dispatching/tests_fleet_bookings_trips.py:276` posts it. Create the test driver with `Driver.objects.create(pk=46, default_flexible=False, default_start_hour=6, default_end_hour=23, …)`, with no weekly rows. That way the stub (06–20, 14h) binds with a non-flexible 20:00 clear-by unless the regular shift bypasses it.
  - `test_switch_off_no_regular_keys_reach_guards`: confirmed driver 46, switch off. Spy on `fg.get_effective_window`: no `configured` argument has `source`.
  - `test_evening_regular_shift_takes_late_job`: switch on. Driver 46 is confirmed Evening 14:15–02:15 every day. A 22:30 pickup at "Disney Resort" going to "MCO Terminal" is assigned to driver 46. With the switch off, the stub's 20:00 end refuses it, which is also asserted.
  - `test_evening_regular_shift_refuses_early_job`: a 13:30 pickup is not assigned to 46.
  - `test_morning_lead_refuses_disney_at_start`: Morning 04:35. A 05:00 Disney pickup is refused and a 05:00 MCO pickup is taken.
  - `test_modal_retyped_hours_keep_hours_and_skip_stub`: the payload hours are `(4, 22)` for driver 46, whose regular shift is 04:10–15:30. `capped_windows[46]` has `start == 4`, `end == 22`, `source == "regular"` and no `start_min`.
  - `test_conflict_advisor_tags_regular_not_stub`
  - `test_day_roster_returns_regular_keys_when_on`
  - `test_capacity_planner_suggestions_use_regular_window`
- [ ] **Step 2: Run the tests and confirm they fail.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_regular_shift_engine`.
- [ ] **Step 3: Implement the wiring listed under Files.**
- [ ] **Step 4: Run the full suite.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching drivers --parallel 4`. Expected: no new failures. The known environment failures listed in commit 86d34115 are acceptable. Re-run any other failure 3 times on this commit before calling it real.
- [ ] **Step 5: Run the parity gate.** It must pass before you commit:

```bash
SP=C:/Users/abdia/AppData/Local/Temp/claude/C--Users-abdia-OneDrive-Desktop-grayson-towncar/2431fe51-eacf-49ef-9f27-a80c965f0151/scratchpad
GRAYSON_SNAPSHOT_DB=$SP/snapshot-2026-10-04.sqlite3 PIPELINE_GATE_TMP=$SP/gate_after ENABLE_DEBUG_TOOLBAR=0 \
  python docs/scheduling-redesign/analysis/14_pipeline_parity.py --tag stage1_after \
  --baseline $SP/14_pipeline_parity_stage1_before.json
mv docs/scheduling-redesign/analysis/out/14_pipeline_parity_stage1_after.json $SP/
```

  Expected output: `differences : 0` and the verdict "PASS — byte-identical". Any difference blocks the task until it is explained.
- [ ] **Step 6: Commit.** Subject: "Every auto-assign pass reads a regular shift to the minute when the switch is on". The body records the parity result. End the body with `Release-Note: none`.

---

### Task 6: Driver profile — Shift facts card and fields (first visible change)

**Files:**
- **Modify `drivers/forms.py` `DriverProfileForm`.** Add to `Meta.fields`: `hard_earliest_start`, `hard_latest_finish`, `hard_latest_finish_next_day`, `max_days_per_week`, `extra_shift_days` and `preferred_vehicles`.
  - Widgets:
    - The two times: `forms.TimeInput(attrs={"type": "time"}, format="%H:%M")`
    - Days a week: `forms.NumberInput(attrs={"min": 1, "max": 7})`
  - `extra_shift_days` is a `TypedMultipleChoiceField(coerce=int, choices=DriverWeeklySchedule.DAY_CHOICES, widget=CheckboxSelectMultiple, required=False)`.
  - `preferred_vehicles` is a `ModelMultipleChoiceField(widget=CheckboxSelectMultiple, required=False)` over active `FleetVehicle` rows, ordered by `vehicle_number`. Widen its queryset with the driver's current units, the same way `certified_vehicle_types` does.
  - `clean()`: when the instance has a regular shift, run `regular_shifts.limit_messages(current_days(instance), …cleaned values…)` and add each message as a non-field error.
- **Create `drivers/templates/drivers/_shift_facts_card.html`.** It is read-only and visible to all staff. It shows:
  - a 7-day grid from the view's `regular_rows` (`[(day_name, label)]` via `day_label`), or "No regular shift yet" when there is none;
  - a "confirmed by X on Oct 4" line;
  - "Never starts before", "Never finishes after" (with "(next day)" when it applies), "Days a week", "Open to extra shifts on" and "Regular car", each showing "—" when blank;
  - under Regular car, the hint "Day Setup offers this car first."

  There are no links to the Task 7 and 8 pages yet; Task 7 adds them.
- **Modify `drivers/templates/drivers/driver_profile.html`.** Include the card in the left column, above the Weekly Schedule card, in both modes. In edit mode, add a fourth form card, "Shift facts", in the page's manual field style.
- **Modify `drivers/views.py` `driver_profile`.** Add `regular_rows` and `regular_summary` to the context.
- **Modify `dispatching/day_setup.py:569`.** Change `"his car (set in admin)"` to `"his regular car"` (S13), and update any test that pins the old text.
- **Create `docs/release-notes/2026-10-04-regular-shifts-and-driver-facts.md`** from `_TEMPLATE.md`, audience Dispatchers. Draft, then tighten to the README's rules:

> Hey team — every driver's profile now has a Shift facts card: their regular shift for each day, the earliest they'll ever start and the latest they'll ever finish, how many days a week they work, which days they'll take an extra shift, and their regular car.
>
> 1. Drivers → Regular Shifts lists everyone who still needs one, with a suggestion built from their last 8 weeks.
> 2. A manager opens a driver, checks each day, and presses Confirm.
> 3. The Morning, Midday and Evening shapes live on Shift Templates (linked from that page), each capped at 12 hours.
>
> Only managers can change these; everyone can see them.
>
> Auto-assign keeps using today's hours until a manager switches it over. Day Setup still offers a driver his regular car first, as it does today. Nothing is sent to drivers and the driver app is the same.

- **Test:** extend `drivers/tests_shift_facts.py`:
  - `test_profile_shows_shift_facts_card_to_dispatcher`
  - `test_manager_saves_facts`: POST the full flat dict, as `DriverProfileEditTests` does (`drivers/tests.py:460`).
  - `test_saving_other_field_keeps_preferred_vehicles`: the driver prefers an inactive unit; `?edit=1` shows it checked; the POST includes it and a changed phone number; the unit is kept.
  - `test_hard_limit_conflict_names_the_day`: Monday is confirmed with a 04:10 start; POST `hard_earliest_start=05:00`. `resp.context["driver_form"].non_field_errors()` contains "Monday: starts at 4:10 AM — before this driver's earliest start (5 AM).", and nothing is saved (Review Focus 5).
  - `test_dispatcher_post_forbidden`
- [ ] Steps:
  1. Write the failing tests and run them; they should fail.
  2. Implement.
  3. Run `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers dispatching.tests_day_setup`; it should pass.
  4. Commit, including the release note. Subject: "Every driver's profile shows their regular shift, hard limits, days a week and regular car".

---

### Task 7: Regular Shifts list, editor and the switch

**Files:**
- **Create `drivers/regular_shift_views.py`** with three views:
  - `regular_shifts(request)`: GET, staff.
  - `regular_shift_edit(request, driver_id)`: GET for staff; POST for managers only, 403 for anyone else.
  - `regular_shift_switch(request)`: POST, managers, `require_POST`.
- **Modify `drivers/urls.py`.** Add:
  - `regular-shifts/` → `regular_shifts`
  - `<int:driver_id>/regular-shift/` → `regular_shift_edit`
  - `regular-shifts/switch/` → `regular_shift_switch`
- **Modify `drivers/forms.py`.** Add `RegularDayForm(forms.Form)` with:
  - `template`: a `ChoiceField`, with `""` meaning Off, plus the template ids
  - `start` and `end`: optional `TimeField`s, time widget

  Build the formset with `formset_factory(RegularDayForm, extra=0)` (7 forms). Its `clean()` fills blank times from `band_fill` when a shape is picked, then runs `validate_regular_shift`.
- **Create `drivers/templates/drivers/regular_shifts.html`.** Sections:
  1. **Status panel.** "Auto-assign uses: today's hours" or "Auto-assign uses: regular shifts".
     - Managers get the button "Use regular shifts for auto-assign". It is disabled, with "N drivers still need a regular shift", until the list is empty.
     - When the switch is on, the button reads "Go back to today's hours".
     - A line explains that switching changes only auto-assign's hours.
  2. **"Needs a regular shift (N)".** Each driver shows the suggestion's `summary_label` (or "no trips in the last 8 weeks") and, for managers, "Review & confirm".
  3. **"Confirmed (M)".** Each driver shows the summary, "confirmed by X on Oct 4", and an Edit link.
  4. A "Shift Templates" link is added in Task 8.
- **Create `drivers/templates/drivers/regular_shift_edit.html`.**
  - The header shows the name and the hard limits.
  - Each weekday row shows the shape select, start, end, an evidence line ("Worked 6 of the last 8 Mondays · usual 4:35 AM – 3:35 PM" or "Not a regular day"), and the band hint.
  - A Confirm button submits the page.
  - Prefill from `current_days` when the driver is confirmed, otherwise from the suggestion.
  - When a shape is picked with blank times, inline JS fills `band_fill` (passed as JSON). The server does the same if JS is off.
  - A valid POST calls `save_regular_shift`, adds each `band_warnings` message as `messages.warning`, adds `messages.success("Regular shift confirmed for {driver}.")`, and redirects to `regular_shifts`.
- **Modify `_shift_facts_card.html`.** Managers get a "Set regular shift" / "Edit regular shift" link to `regular_shift_edit`.
- **Modify `dispatcher_navbar.html`.** In the Drivers dropdown, after "Edit Schedules", add `<i class="bi bi-calendar2-check me-2"></i>Regular Shifts` for all staff. Add `regular_shifts` and `regular_shift_edit` to the dropdown's active-state list.
- **Update the release note** if the steps changed. The commit carries `Release-Note: none (covered by docs/release-notes/2026-10-04-regular-shifts-and-driver-facts.md)`.
- **Test:** extend `drivers/tests_regular_shifts.py`:
  - `test_list_splits_needs_and_confirmed`
  - `test_list_shows_no_recent_trips`
  - `test_editor_prefills_from_suggestion`
  - `test_manager_confirms`
  - `test_editor_rejects_13h_day`: assert on the formset errors, and check nothing is saved.
  - `test_editor_fills_blank_times_from_band`
  - `test_dispatcher_cannot_post_editor` (403)
  - `test_switch_button_disabled_until_empty`
  - `test_switch_post_refused_with_message`
  - `test_switch_post_turns_on`
  - `test_switch_post_dispatcher_forbidden`
  - `test_new_driver_after_switch_on_listed_switch_stays_on` (Review Focus 3)
  - `test_navbar_link`
  - `test_profile_card_links_to_editor_for_manager`
- [ ] Steps: failing tests → run (fail) → implement → `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers` (pass) → commit. Subject: "Managers confirm each driver's regular shift from a suggested pattern, and switch auto-assign over when everyone has one".

---

### Task 8: Shift Templates page

**Files:**
- **Modify `drivers/regular_shift_views.py`.** Add `shift_templates(request)`: GET for staff (read-only unless the user is a manager), POST for managers.
- **Modify `drivers/urls.py`.** Add `shift-templates/` → `shift_templates`.
- **Modify `drivers/forms.py`.** Add `ShiftTemplateForm(ModelForm)`:
  - `Meta.fields = ["start_earliest", "start_latest", "end_earliest", "end_latest", "notes"]`. `max_span_minutes` is deliberately left out.
  - Add `max_span_hours = forms.DecimalField(min_value=1, max_value=12, decimal_places=1, step_size=0.5, error_messages={"max_value": "No shift can be longer than 12 hours."})`.
  - Set its initial value in `__init__` to `instance.max_span_minutes / 60`.
  - In `clean()`, set `instance.max_span_minutes = int(hours * 60)`. Refuse a value below the longest confirmed day using that template, with the message `"{n} confirmed regular shifts on {Name} are longer than that — edit them first."`.
  - Use it with `modelformset_factory(ShiftTemplate, form=ShiftTemplateForm, extra=0, can_delete=False)`.
- **Create `drivers/templates/drivers/shift_templates.html`.**
  - Three cards: Morning, Midday, Evening. Each shows the band fields, "Longest shift (hours)", the notes, and "used by N drivers" linking to the list.
  - Intro: "These are targets, not limits — a regular shift outside them is allowed with a warning."
  - Saving calls `clear_template_cache()`, sets `updated_by`, and shows "Shift templates saved."
- **Modify `regular_shifts.html`.** Add the "Shift Templates" link, and add `shift_templates` to the navbar's active-state list.
- **Test:** `drivers/tests_shift_templates.py`, using the mixin:
  - `test_staff_sees_read_only`
  - `test_manager_edits_band`
  - `test_over_12_hours_refused`: formset errors contain the message, and the DB is unchanged.
  - `test_lowering_below_confirmed_shift_refused`
  - `test_save_clears_template_cache`
  - `test_dispatcher_post_forbidden`
  - `test_initial_shows_hours_not_minutes`: the `max_span_hours` field's initial value is 12.
- [ ] Steps: failing tests → run (fail) → implement → run (pass) → commit with the same trailer as Task 7. Subject: "Managers tune the Morning, Midday and Evening shapes on a Shift Templates page".

---

### Task 9: Whole-branch verification (controller)

- [ ] **Full suite.** Run `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching drivers --parallel 4`. Record N tests and the failures. Each failure must be either a known environment failure or shown to be flaky by 3 reruns.
- [ ] **Parity gate.** Re-run it (Task 5, Step 5) on the final commit and record `differences : 0`.
- [ ] **Switch-on smoke test.** Use a throwaway migrated copy of the snapshot.
  - Confirm three drivers (Morning, Midday, Evening) and turn the switch on.
  - Preview Auto-Assign for one busy date.
  - Record:
    - the evening driver keeps a late job;
    - no regular window rejects every pickup;
    - every regular driver's planned base→base is ≤ 12h;
    - the count of 00:00–02:59 legs (S14).
- [ ] **Browser pass.** Cover the profile card plus edit, Regular Shifts, the editor (confirm one driver end to end), the switch, and Shift Templates. Check desktop and 375px, with screenshots.
- [ ] **Migrations.** `python manage.py makemigrations --check --dry-run` must print "No changes detected".
- [ ] **Release note.** Re-read it against `docs/release-notes/README.md`: under 150 words, no field names, and it says what did not change.
