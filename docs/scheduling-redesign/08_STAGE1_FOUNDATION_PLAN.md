# Stage 1 — Foundation: Implementation Plan (rev 4)

> **Rev 4 (2026-10-04, founder clarifications; design 07 §13):**
> - Stage 1 stays focused on driver facts, regular shifts, availability, minute precision and cross-midnight foundations. It adds no vehicle assignment and no trip optimization, and Day Setup is untouched.
> - **Drive-only base→base (K10).** The Stage 1 lead/tail is just the drive to and from base plus the pickup buffer. The fuel (15), report/prep (25) and wash (61) allowances are removed. The founder's new handover timing (40-min return including an optional wash, 10-min takeover, wash skipped when it costs a trip; K7–K8) becomes a Stage 3 setting.
> - **Strict 12h (S20).** A regular-shift driver whose day is already over 12h base→base takes no further planned trip.
> - Both changes land in the new **Task 4b**, which revises code built in Tasks 1, 3 and 3c.

> **Rev 3 (2026-10-04, founder request mid-build):**
> - Every driver gets a **usual shift**: Morning, Midday, Evening or **Float** (any shape, fills gaps).
> - Single days can differ from the usual shift: a different shape, "Morning *or* Evening", or a per-day limit such as "Thursday: done by 3 PM".
> - Profiles gain a **driver knowledge base** for people to read (the engine does not use it yet):
>   - strengths and habits as tags;
>   - a dated log of compliments, complaints, incidents and **strikes**.
> - New tasks: **3b**, **3c**, **9** and **10**. The old Task 9 (verification) is now **Task 11**.
> - Decisions **S15–S19**.
> - Tasks 1–3 were already built and are unchanged.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store each driver's regular shift and hard limits to the minute, including shifts that cross midnight. Add a switch that lets those regular shifts replace the hard-coded "observed-history" driver windows.

**Architecture:**
- The rules door (`dispatching/feasibility_guards.py`) learns minute-precision windows that can cross midnight. These ride as optional `start_min`/`end_min`/`kind` keys and are checked base → base: the drive from base plus the pickup buffer, and the drive back to base (drive-only in Stage 1, §13 K10), all come from `handoff_chain`. Hour windows behave exactly as before.
- Driver facts live on `Driver`. Regular shifts live in new fields on `DriverWeeklySchedule` that point at a new `ShiftTemplate`. Their logic lives in a new module, `drivers/regular_shifts.py`.
- The availability door (`drivers/availability.py`) turns a confirmed regular shift into a 12-hour window only when `SchedulerSettings.regular_shift_windows` is on. The engine's window builders pass that window through.
- A profile card and three staff pages let managers set it all up.

**Tech Stack:** Django 5.1.4, Python 3.13, SQLite (dev and tests), Postgres (production), Bootstrap 5.3, server-rendered templates.

**Spec:** [07_STRUCTURED_SHIFTS_DESIGN.md](07_STRUCTURED_SHIFTS_DESIGN.md), especially:
- §2 rows U1, U2, U3, U5, U11, C2, C3, C7, C8, C10, C11 (U14, U16 and U19 are superseded or open: §13 K7, K8, O2, O5)
- §13, the founder clarifications, especially K10 (what Stage 1 does and does not do)
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
| S5 | **Times are base → base (U2), drive-only in Stage 1 (§13 K10)**, through one pair of helpers in `handoff_chain`:<br>• `shift_lead_min(kind, pickup_zone)` = base→zone drive (central `BASE_TO_ZONE`) + pickup buffer (10 airport / 15 other).<br>• `shift_tail_min(kind, drop_zone)` = drive from the drop zone back to base (central).<br>`kind` stays in both signatures but doesn't change the result in Stage 1. Stage 3 plugs in there: the handover settings (K7: 40-min return including an optional wash, then a 10-min takeover; K8: skip the wash when it would cost a trip) and the still-open end-of-night return (O2).<br>The engine checks `pickup − lead ≥ window start` and `clear + tail ≤ window end`. Pre-fill uses the same helpers. *(Rev 3 had fuel 15, report 25 and wash 61 here; Task 4b removes them.)* |
| S6 | **Pre-fill (C8)** uses the 56 days before today:<br>• A pickup before 02:00 finishes the previous day **only when that day was worked** (it had a pickup at 02:00 or later). Otherwise it is early work on its own date, so a night-only day stays on its own weekday instead of becoming an Evening about 24h too early. Today's pickups before 02:00 count for yesterday.<br>• Every leg of a worked day is held to the engine's base → base rule (S5), so a suggestion never turns down a leg the driver does every week. A day's raw start is the earliest, over its legs, of pickup − `shift_lead_min("morning", that pickup's zone)`, and its template is the one whose start band is nearest that raw start (Float is never suggested: its band covers the whole day). A raw start before midnight is held at 00:00.<br>• A day's end is the latest, over its legs, of P50 occupancy end + `shift_tail_min(kind, that leg's drop zone)`, i.e. plus the drive back to base. *(Rev 4: no evening report offset, no fuel, no wash.)*<br>• A weekday is regular when it was worked in at least 4 of the 8 weeks. Its template is the one most of those days had; a tie goes to the template nearest the median raw start.<br>• Start and end are medians over the days of that template. Start rounds **down** to 5 min and end rounds up to 5 min. The end is capped at start + the template's max span.<br>*(Amended after rev 4 to record the pre-fill as built in Task 3's review fixes: the night rule above, every leg rather than the first pickup and latest end, and medians over the majority template's days. Lead and tail stay drive-only, K10.)* |
| S7 | `extra_shift_days` is the list of weekdays the driver is open to an extra shift. Empty means not open. `hard_latest_finish` is a time plus a `hard_latest_finish_next_day` flag. |
| S8 | **Template bands are targets (U11).** A shift outside its band gets a warning. A shift longer than `max_span_minutes` (≤ 720) is an error. The seed comes from §3. The lower end of Midday's end band (15:00) and of Evening's end band (20:00) are choices made in this plan, not values from §3. |
| S9 | **Hour-only paths are unchanged in Stage 1**, and they never read availability: the farm-out optimizer's worked-span windows (`farmout_optimizer.py:792-807`) and `fleet_intel.py:374` (`configured=None`). Where an Auto-Assign modal hour was retyped by a dispatcher, the typed hours win: that driver keeps an hour window, still bypasses the stub (`"source": "regular"`), and gets no minute keys. |
| S10 | Managers (`is_superuser`) edit regular shifts, hard limits and templates. Every staff user can view them. |
| S11 | Validation checks rest both ways between consecutive working days, Sunday→Monday included, using `SchedulerSettings.rest_min_gap_minutes` (510, C7). |
| S12 | *(withdrawn in rev 2: the hour path stays byte-for-byte, wrapping windows included)* |
| S13 | **Stage 1 does not change Day Setup at all** (§13 K10). C3 lands in Stage 2 with weekly pairing (U23/C14). A regular car set on the profile stays Day Setup's first choice, as it already is when set in admin, and Day Setup's label stays as it is. *Accepted by the founder on 2026-10-05: Day Setup's code is untouched, but it reads driver hours through the availability door. With the switch on, it therefore follows each driver's regular days on and off and uses his regular start time when ordering a shared car. The Regular Shifts page and the release note say so.* |
| S15 | **Usual shift per driver.** `Driver.shift_role` is a FK to `ShiftTemplate` (null means not set). It is shown as "Morning driver", "Evening driver", "Midday driver" or "Float — any shift". The editor sets it first. Each working day starts out as the usual shift and can be overridden on its own. The rows remain the source of truth; `shift_role` is the default and the label. |
| S16 | **Float** is a fourth `ShiftTemplate` row: kind `float`, start band 03:00–16:00, end band 12:00–02:15, max span 720. A Float day means "any shape, wherever the day needs him", still within 12h and his limits. |
| S17 | **Per-day options.** Each weekday row gains:<br>• `alt_template`: a second allowed shape, for "Morning or Evening".<br>• Per-day limits: `day_earliest_start`, `day_latest_finish` and `day_latest_finish_next_day`, for "Thursday: done by 3 PM".<br>`shift_start` and `shift_end` become optional. Leaving both blank means the shape's usual times (`band_fill`); filling in only one of them is an error. |
| S18 | **Window with the switch on**, computed in one place (`regular_shifts.regular_window`):<br>• **Single fixed shape:** the S4 rule. Times come from the row, or from `band_fill` when blank.<br>• **Float, or two shapes:** from the earliest allowed `start_earliest` to the latest allowed `end_latest`.<br>• **Then:** clip by the driver's hard limits **and** the day's limits.<br>• **Every regular window** carries `max_span_min` (the template's `max_span_minutes`; for two shapes, the smaller of the two). The rules door checks span base → base: first pickup − lead to last clear + tail must be ≤ `max_span_min` (Task 3c). This keeps Float and two-shape days at 12h too.<br>• **Lead and tail** are the drive-only S5 values for every kind, Float included. |
| S19 | **Driver knowledge is for people in Stage 1.** The engine does not read it.<br>• **Tags:** `DriverTag` vocabulary with category strength / habit / language / area and polarity positive / caution, plus `DriverTagAssignment`.<br>• **Log:** `DriverLogEntry` with kind compliment / complaint / incident / note, a strike flag, optional trip, and who/when.<br>• **Who can do what:** any staff user can add tags and log entries; only managers can mark a strike, remove a tag, or edit or delete an entry. Strikes are counted over the last 12 months.<br>• **Visibility:** staff-only. Never on any driver-facing page. |
| S14 | **Accepted Stage 1 limit:** the engine judges each date's legs against that date's window only. With the switch on, an evening driver's after-midnight work (pickups 00:00–02:59 on the next date's board) can't go to him. Stage 3 builds cross-date shifts. The smoke test reports how many legs this affects. |
| S20 | **No planned work into an overrun (§13 K9, K10).** When a regular window's day is already over `max_span_min` base→base before a leg is added, the minute path refuses the leg, even one that would not lengthen the day. The refusal reason is `"day already over 12h 0m base to base"`, filled in with the window's limit. This replaces Task 3c's delta exception for minute windows. The hour path's max-hours delta rule stays as it is, for parity. A dispatcher's own moves still only warn (§6.5). |

## Global Constraints

- **Switch-off parity.** Every new engine behaviour sits behind `SchedulerSettings.regular_shift_windows` (default False). With the switch off, `docs/scheduling-redesign/analysis/14_pipeline_parity.py` must report `differences : 0` against `$SP/14_pipeline_parity_stage1_before.json`. The hour path of `window_check` stays byte-for-byte, wrapping windows included.
- **12h ceiling.** `ShiftTemplate.max_span_minutes` ≤ 720. A regular-shift day may not exceed its template's `max_span_minutes`. The switch-on window is never wider than `max_span_minutes`.
- **Data.** Times are stored with minute precision in **new** fields only. `DriverVehicleAssignment.planned_*` stays unwritten.
- **Scope (§13 K10).** Don't build vehicle assignment, car sharing, handover timing or trip optimization in Stage 1, and don't change Day Setup.
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
- **Release notes.** There are two notes, one per shipped change. Commits with no UI carry `Release-Note: none`.
  - **Shifts:** `docs/release-notes/2026-10-04-regular-shifts-and-driver-facts.md`, created in Task 6. UI commits in Tasks 7–8 update it and carry `Release-Note: none (covered by docs/release-notes/2026-10-04-regular-shifts-and-driver-facts.md)`.
  - **Knowledge:** `docs/release-notes/2026-10-04-driver-strengths-habits-and-log.md`, created in Task 9. Task 10 updates it and carries `Release-Note: none (covered by docs/release-notes/2026-10-04-driver-strengths-habits-and-log.md)`.
- **Staff-only data.** Driver tags and log entries must never appear on any driver-facing page or API (`drivers/views.py` driver portal, `operator_views.py`). Add a test that the driver app's pages don't contain them.
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
| `dispatching/feasibility_guards.py` | Minute path in `window_check` (lead/tail); regular windows bypass the stub; `regular_window_keys(eff)`; `legacy_hours()`; manual-sovereign regular windows get a warn-only base→base span (Task 5) |
| `dispatching/handoff_chain.py` | `base_drive_min`, `shift_lead_min`, `shift_tail_min` (drive-only after Task 4b) |
| `drivers/models.py` + `drivers/migrations/0062_shift_facts.py`, `0063_seed_shift_templates.py` | `ShiftTemplate`; `Driver` facts; regular-shift fields on `DriverWeeklySchedule`; `SHIFT_TEMPLATES_CACHE_KEY` |
| `dispatching/models.py` + `dispatching/migrations/0022_schedulersettings_regular_shift_windows.py` | The switch, `GUARDED_FIELDS`, `REGULAR_WINDOWS_CACHE_KEY` |
| `drivers/test_support.py` (new) | `RegularShiftCacheMixin` |
| `drivers/regular_shifts.py` (new) | Switch read/write, roster, suggestions, validation, save, labels |
| `drivers/availability.py` | Regular-shift keys; the window when the switch is on; labels; minute-aware `is_pickup_within_window` |
| `dispatching/{assignment_pipeline,scheduler,swap_optimizer,board_validation,conflict_advisor,conflict_advisor_actions,farmout_actions,day_planner,views}.py` | Pass the regular keys to every window builder |
| `drivers/forms.py`, `drivers/regular_shift_views.py` (new), `drivers/urls.py`, templates | UI |
| `drivers/driver_knowledge.py`, `drivers/driver_knowledge_views.py` (new); `DriverTag`, `DriverTagAssignment`, `DriverLogEntry`; migrations 0066–0068 | Driver knowledge (Tasks 9–10) |

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
  - *(Rev 4: Task 4b removes the next two constants.)* `HANDOVER_FUEL_MIN = 15`
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

### Task 3b: Usual shift, Float and per-day options (models + module)

**Files:**
- Modify: `drivers/models.py`
  - `ShiftTemplate.KIND_CHOICES` gains `("float", "Float")`.
  - `Driver.shift_role` = FK `ShiftTemplate`, null, blank, PROTECT, `related_name="role_drivers"`. Help text: "The driver's usual shift: Morning, Midday, Evening, or Float for anything."
  - `DriverWeeklySchedule` gains:
    - `alt_template`: FK `ShiftTemplate`, null, blank, PROTECT, `related_name="+"`
    - `day_earliest_start` and `day_latest_finish`: TimeField, null, blank
    - `day_latest_finish_next_day`: Bool, default False
- Create: `drivers/migrations/0064_usual_shift_and_day_options.py` (schema) and `drivers/migrations/0065_seed_float_template.py` (data). The seed is kind `float`, name "Float", start band 03:00–16:00, end band 12:00–02:15, max span 720, sort 4, note "Any shape — goes wherever the day needs him, still within 12 hours and his limits."
- Modify: `drivers/regular_shifts.py`
- Test: extend `drivers/tests_regular_shifts.py`, and update any Task 3 test whose expectation 3b deliberately changes (blank times are now valid).

**Interfaces:**
- Consumes: Task 3's module and Task 2's models.
- Produces:

```python
@dataclass(frozen=True)
class DayShift:                      # new fields appended with defaults; old call sites keep working
    day: int; template_id: Optional[int]; start: Optional[time]; end: Optional[time]
    alt_template_id: Optional[int] = None
    day_earliest: Optional[time] = None
    day_latest: Optional[time] = None
    day_latest_next_day: bool = False

@dataclass(frozen=True)
class RegularWindow:
    start_min: int; end_min: int; kind: str; max_span_min: int   # kind in morning|midday|evening|float

def effective_minutes(day: DayShift, templates) -> Optional[tuple[int, int]]   # typed times, else band_fill
def regular_window(day: DayShift, templates, *, hard_lo: Optional[int], hard_hi: Optional[int]) -> Optional[RegularWindow]
def suggest_role(suggestions: list[DaySuggestion], templates) -> Optional[int]
def role_label(driver, templates) -> str        # "Morning driver" / "Midday driver" / "Evening driver" / "Float — any shift" / ""
def save_regular_shift(driver, days, user, *, role_template_id: Optional[int] = None) -> None
```

- **`regular_window`** implements S4 and S18. It is the only place the window is built; Task 4 calls it.
  - Single non-float shape: `(s, e) = effective_minutes`, then morning `(s, s+M)`, evening `(max(0, e−M), e)`, midday `(s, e)`.
  - Float, or a day with `alt_template`: from the earliest `start_earliest` to the latest end-band end, with `kind="float"` and `M = min(max_span_minutes of the shapes)`. Typed times on such a day are labels only.
  - Then clip to `max` of the start-side values `(start, hard_lo, day_lo)` and `min` of the end-side values `(end, hard_hi, day_hi)`. `day_hi` gets +1440 when `day_latest_next_day` is set.
  - `None` for an Off day.
- **`validate_regular_shift` / `limit_messages`** gain these rules (pinned literals):
  - One time blank: `"{Day}: pick a start and an end time, or set the day to Off."`
  - `"{Day}: the second shift must be different from the first."`
  - `"{Day}: Float already covers every shift — no second shift needed."`
  - `"{Day}: starts at {t} — before that day's earliest start ({t})."`
  - `"{Day}: ends at {t} — after that day's finish-by ({t})."`
  - A shape with both times blank is valid. Its 12h check uses `effective_minutes`.
  - Float or two-shape days skip the typed-time span check.
  - The rest check uses each day's `regular_window` and is skipped when either side is a Float or two-shape day; the base→base span check in Task 3c holds those days instead.
- **`suggest_role`** returns the most common template among regular days (ties go to the lower `sort_order`), or None when there are no regular days.
- **`day_label`** examples:
  - `"Morning 4:10 AM – 3:30 PM"`
  - `"Morning (usual times)"`
  - `"Morning or Evening"`
  - `"Float"`
  - day-limit suffixes `" · not before 6 AM"` and `" · done by 3 PM"`, e.g. `"Morning 4:10 AM – 3:30 PM · done by 3 PM"`
- **`save_regular_shift`** writes the new row fields (still only the regular-shift fields, per S1) and `driver.shift_role_id = role_template_id`.

- [ ] **Step 1: Write the failing tests.**
  - `test_float_template_seeded`
  - `test_role_saved_and_labelled`
  - `test_blank_times_use_band_fill`
  - `test_one_time_blank_refused`
  - `test_alt_shape_must_differ`
  - `test_float_cannot_have_alt`
  - `test_day_limit_messages`: Thursday 04:10–15:30 with `day_latest=15:00` gives "Thursday: ends at 3:30 PM — after that day's finish-by (3 PM)."
  - `test_regular_window_morning_evening_midday`: 04:10–15:30 gives (250, 970); 14:15–02:15 gives (855, 1575); midday 07:00–19:00 gives (420, 1140).
  - `test_regular_window_float`: (180, 1575), kind "float", max_span 720.
  - `test_regular_window_morning_or_evening`: (180, 1575), kind "float".
  - `test_regular_window_clipped_by_day_and_hard_limits`: Thursday done by 15:00 gives end 900; hard earliest 04:30 gives start 270.
  - `test_suggest_role_most_common`
  - `test_save_writes_new_fields_only`: legacy fields untouched.
  - `test_day_label_variants`
- [ ] **Step 2: Run the tests and see them fail.** `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_regular_shifts`
- [ ] **Step 3: Implement.** Generate 0064 with `makemigrations` and hand-write 0065.
- [ ] **Step 4: Run the tests and see them pass.** Same command, plus `drivers.tests_shift_facts` and `python manage.py makemigrations --check --dry-run`.
- [ ] **Step 5: Commit.** Subject: "Drivers have a usual shift, Float means any shift, and single days can differ or end early". Trailer: `Release-Note: none`.

---

### Task 3c: The 12-hour base→base span check in the rules door

**Files:**
- Modify: `dispatching/feasibility_guards.py`
  - Regular windows may carry `max_span_min: int`.
  - `window_check` gains `base_span_min_after=None, base_span_min_before=None` (keyword, last).
  - New helper `base_span_min(legs, kind) -> Optional[int]`.
  - `regular_window_keys(eff)` also returns `max_span_min` from `eff["window_max_span_min"]` when that is set.
- Modify: `dispatching/scheduler.py`
  - `check_feasibility` (Guard C, ~:1226-1239) and `_chain_ok` (~:2574-2582): when the window has `start_min` and `max_span_min`, compute the before and after base spans with `fg.base_span_min` over the driver's slots, plus the new leg in `check_feasibility`. Each item is `(datetime.combine(target_date, pickup_time), pickup_category, clear_dt, dropoff_category)`. Pass the results to `window_check`.
- Test: extend `dispatching/tests_minute_windows.py`, and add a `check_feasibility` case to `dispatching/tests_regular_shift_engine.py`. Create that file here if Task 5 has not yet.

**Interfaces:**
- **`base_span_min`:** `base_span_min(legs: Iterable[tuple[datetime, str, datetime, str]], kind: str) -> Optional[int]` returns `max(clear + shift_tail_min(kind, drop)) − min(pickup − shift_lead_min(kind, pick))` in whole minutes, or None for no legs.
- **Minute path:** when `max_span_min` and `base_span_min_after` are both set, reject when `after > max_span_min`, unless the day was already over before the leg and the leg does not make it longer. This is the same delta rule as the hour path's max-hours gate. *(Rev 4: Task 4b makes this strict for minute windows, per S20.)* Reason: `"base to base {H}h {M}m > {h}h {m}m"`, e.g. `"base to base 12h 5m > 12h 0m"`.

- [ ] **Step 1: Write the failing tests.**
  - `test_base_span_helper`: a 05:00 MCO pickup clearing 06:15 at Disney, then 16:00 Disney→MCO clearing 16:40, kind morning → 04:38 → 17:07 = 749.
  - `test_base_span_cap_rejects_over_12h`
  - `test_base_span_allows_hole_fill_when_already_over`
  - `test_no_cap_without_max_span_min`
  - `test_regular_window_keys_includes_max_span`
  - `test_check_feasibility_float_day_held_to_12h`: Float window (180, 1575, 720). Existing slots run 05:00 MCO … 15:00. Adding a 16:30 pickup clearing 17:20 at MCO is refused with the base-to-base reason.
- [ ] **Step 2: Run the tests and see them fail.** `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_minute_windows dispatching.tests_regular_shift_engine`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and see them pass.** Same command, plus `dispatching.tests_feasibility_guards dispatching.tests_span_caps`.
- [ ] **Step 5: Commit.** Subject: "Every regular shift is held to 12 hours from leaving base to getting back". Trailer: `Release-Note: none`.

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
  - `window_start_min`, `window_end_min`, `window_kind` and `window_max_span_min`. These are set **only** when the switch is on.
  - `shift_role_label`, from `regular_shifts.role_label` (Task 3b); `""` for an unconfirmed driver.
- **Switch on, confirmed driver, working day.** Before exceptions are applied, the base layer becomes:
  - `is_available=True` and `flexible=False`
  - `shift_type`: the template's kind, or `"full_day"` for Float and two-shape days. `"full_day"` is an existing `SHIFT_TYPE_CHOICES` value, so shift-type consumers (for example `schedule_risk`) keep working.
  - The window is `regular_shifts.regular_window(day, templates, hard_lo, hard_hi)` (Task 3b), where `hard_lo, hard_hi = driver.hard_window_minutes()`.
  - `(start_hour, end_hour) = fg.legacy_hours(window)`
  - `_classify_status` must return `"fixed_window"` for these days, even when `shift_type == "full_day"`, because `flexible` is False.
- **Switch on, confirmed driver, Off day:** `is_available=False`. `save_regular_shift` makes no weekly row for an Off day, so for a confirmed driver a weekday with no row (`entry is None`) is Off too. It must not fall back to the `default_*` hours.
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
  - `test_switch_on_float_day`: window (180, 1575), `window_kind == "float"`, `window_max_span_min == 720`, `display_label == "Float"`, `status == "fixed_window"`.
  - `test_switch_on_day_limit_clips`: Thursday done by 15:00 gives `window_end_min == 900`; Monday is unaffected.
  - `test_role_label_in_eff`: `shift_role_label == "Morning driver"`.
  - `test_pickup_within_regular_window`
  - `test_no_extra_queries_unconfirmed`: `assertNumQueries(0)` with a cold cache, with weekly rows and overrides prefetched.
  - `test_no_extra_queries_confirmed_warm`: `assertNumQueries(0)` once the caches are warm.
- [ ] **Step 2: Run the tests and confirm they fail.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_availability_regular`.
- [ ] **Step 3: Implement.** Keep `_weekly_or_defaults` unchanged. Add `_apply_regular(base, driver, entry, templates)`.
- [ ] **Step 4: Run the tests and confirm they pass.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_availability_regular drivers.tests drivers.tests_regular_shifts`.
- [ ] **Step 5: Commit.** Subject: "With the switch on, a confirmed regular shift is the driver's hours for the day". End the body with `Release-Note: none`.

---

### Task 4b: Drive-only base→base allowances and strict 12h (rev 4)

This applies §13 K10, together with S5, S6, S18 and S20, to code already built in Tasks 1, 3 and 3c. It adds no new feature.

**Files:**
- `dispatching/handoff_chain.py`
  - Delete `HANDOVER_FUEL_MIN` and `EVENING_REPORT_LEAD_MIN`.
  - `shift_lead_min(kind, pickup_zone)` returns `base_drive_min(pickup_zone) + pickup_buffer_min(pickup_zone)`.
  - `shift_tail_min(kind, drop_zone)` returns `base_drive_min(drop_zone)`.
  - Rewrite the comment block to say:
    - Stage 1 counts the drive only.
    - Stage 3 adds two settings (07 §13 K7–K8): `handover_return_min` 40, from MCO, including an optional wash that is skipped when it would cost a trip; and `handover_takeover_min` 10.
    - The end-of-night return and return times from other locations are still open (O2, O6).
  - Leave `CHAIN_COMPONENTS`, `car_ready_min` and every other shipped helper exactly as they are.
- `dispatching/feasibility_guards.py`, minute path only (S20):
  - If `base_span_min_before > max_span_min`, refuse with `"day already over {h}h {m}m base to base"`.
  - Otherwise refuse when `after > max_span_min`, with the existing reason.
  - Do not touch the hour path.
- `drivers/regular_shifts.py`: pre-fill per S6.
  - Drop the Evening `− EVENING_REPORT_LEAD_MIN` offset.
  - The end tail becomes drive-only through `shift_tail_min`.
  - Fix the docstrings that mention the 25-min report or the night return.
- Tests:
  - Update every pinned value in `dispatching/tests_minute_windows.py` (lead/tail, tail examples, base span, `chain_ok`), `dispatching/tests_regular_shift_engine.py` and `drivers/tests_regular_shifts.py` (suggestion start and end times).
  - Add the S20 tests below.
  - Work out each new expected value from the drive-only rule. Never edit a value down to match what the code outputs.

**Expected values (drive-only):**
- `shift_lead_min("morning", "MCO Terminal") == 22`
- `shift_tail_min("morning", "MCO Terminal") == 12`
- `shift_tail_min("evening", "MCO Terminal") == 12`
- `shift_lead_min("evening", "Disney Resort") == 50`
- Tail example, window end 16:35: a 16:23 clear passes. A 16:24 clear fails with `"clears 16:24, back at base 16:36, after 16:35"`.
- Cross-midnight tail, window end 02:15 the next day: a 02:03 clear passes. A 02:04 clear fails with `"clears 02:04, back at base 02:16, after 02:15 (next day)"`.
- `base_span_min` for legs (05:00 MCO→Disney, clear 06:15) and (16:00 Disney→MCO, clear 16:40), any kind: 04:38 → 16:52 = **734**.
- Pre-fill: 05:00 MCO first pickups still suggest a start of `time(4, 35)`.

**New tests:**
- `test_s20_refuses_leg_inside_overrun_day`: with `max_span_min` 720, before = 735 and after = 735, the leg is refused with `"day already over 12h 0m base to base"`.
- `test_s20_hour_path_delta_rule_unchanged`: the hour path still allows a hole-fill on an over-cap day.
- `test_no_handover_constants_left`: `hasattr(hc, "HANDOVER_FUEL_MIN")` and `hasattr(hc, "EVENING_REPORT_LEAD_MIN")` are both False.
- `test_shipped_chain_unchanged`: `car_ready_min("MCO Terminal")[1]` rounds to 61, and `clear_to_pickup_min("MCO Terminal", "MCO Terminal")` is unchanged, so the shipped bands are untouched.

- [ ] **Steps:**
  1. Update and add the tests.
  2. Run them and confirm they fail.
  3. Implement.
  4. Run `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_minute_windows dispatching.tests_regular_shift_engine dispatching.tests_feasibility_guards dispatching.tests_span_caps dispatching.tests_standby_mints dispatching.tests_day_setup drivers.tests_regular_shifts drivers.tests_availability_regular` and confirm they pass.
  5. Commit with the subject "Stage 1 counts only the drive to and from base, and never plans more work into a day already over 12 hours" and the trailer `Release-Note: none`.

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
- **A dispatcher's own moves still only warn (S20, §13 K10, §6.5).** Four of the sites above resolve with `enforce_cap=False`: `board_validation.py:592`, `conflict_advisor_actions.py:450`, `farmout_actions.py:406` and `views.py:4356`. `get_effective_window(enforce_cap=False)` passes `max_span_min` through, and `check_feasibility` computes the base→base span whenever a window has `start_min` and `max_span_min`. Once the regular keys reach these sites, S20 would hard-refuse a dispatcher's own move onto a regular day already over 12h base→base, which only warns today. Fix it in the rules door:
  - In `get_effective_window`, when `enforce_cap` is False and `configured["source"] == "regular"`, rename `max_span_min` to `span_warn_min`, keeping the same value.
  - `check_feasibility` computes the base→base span for `span_warn_min` the same way it does for `max_span_min`. It adds the S20 reason (`"day already over 12h 0m base to base"`, or `"base to base … > 12h 0m"`) to `warnings` and does not refuse.
  - The engine's sites (`enforce_cap=True`, including `swap_optimizer.py:222` and the conflict advisor's generation windows) keep `max_span_min` and still refuse.
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
  - `test_float_driver_takes_morning_and_evening_work_within_12h`: Float driver 46 takes a 05:00 MCO arrival and a 16:30 departure only if the base-to-base day stays ≤ 12h. Otherwise the later one goes to someone else or to the farm list.
  - `test_manual_move_onto_overrun_regular_day_only_warns`: switch on. Driver 46's confirmed regular day is already over 720 min base→base. `check_driver_feasibility` for a leg that fits inside the day returns `feasible` true, with `"day already over 12h 0m base to base"` in `warnings`. A manual swap revalidated through `board_validation` is allowed too.
  - `test_engine_refuses_same_leg_on_overrun_regular_day`: the same board and leg through `build_smart_schedule` or `suggest_assignments` is not seated, with the reason `"day already over 12h 0m base to base"`.
- [ ] **Step 2: Run the tests and confirm they fail.** Run: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_regular_shift_engine`.
- [ ] **Step 3: Implement the wiring listed under Files.**
- [ ] **Step 4: Run the full suite.** Run it serially: `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching drivers`. Don't use `--parallel`: `tblib` is not installed, so the first known environment failure aborts a parallel run with `TypeError: cannot pickle traceback object` and nothing gets reported. Don't add `tblib` as a dependency on this branch. Expected: no new failures. The known environment failures listed in commit 86d34115 are acceptable. Re-run any other failure 3 times on this commit before calling it real.
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
  - a header pill with the usual shift (`role_label`, e.g. "Morning driver" or "Float — any shift");
  - a 7-day grid from the view's `regular_rows` (`[(day_name, label)]` via `day_label`, including "Morning or Evening" and "· done by 3 PM"), or "No regular shift yet" when there is none ("Regular shifts are for in-house chauffeurs only." for an affiliate or an operator, who never gets one);
  - a "confirmed by X on Oct 4" line;
  - "Never starts before", "Never finishes after" (with "(next day)" when it applies), "Days a week", "Open to extra shifts on" and "Regular car", each showing "—" when blank;
  - under Regular car, the hint "Day Setup offers this car first."

  There are no links to the Task 7 and 8 pages yet; Task 7 adds them.
- **Modify `drivers/templates/drivers/driver_profile.html`.** Include the card in the left column, above the Weekly Schedule card, in both modes. In edit mode, add a fourth form card, "Shift facts", in the page's manual field style.
- **Modify `drivers/views.py` `driver_profile`.** Add `regular_rows` and `regular_summary` to the context.
- *(Rev 4: the Day Setup label change is dropped. Day Setup stays untouched, per S13.)*
- **Create `docs/release-notes/2026-10-04-regular-shifts-and-driver-facts.md`** from `_TEMPLATE.md`, audience Dispatchers. Draft, then tighten to the README's rules:

> Hey team — every driver's profile now has a Shift facts card. It shows whether they're a Morning, Midday, Evening or Float driver (Float means any shift), each day of their regular week (including "Morning or Evening" days and "done by 3 PM" days), the earliest they'll ever start and the latest they'll ever finish, how many days a week they work, which days they'll take an extra shift, and their regular car.
>
> 1. Drivers → Regular Shifts lists everyone who still needs one, with a suggestion built from their last 8 weeks.
> 2. A manager picks the driver's usual shift and the days they work, adjusts any day that's different, and presses Confirm.
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
- **Modify `drivers/forms.py`.** Add two forms.
  - `RegularShiftForm(forms.Form)` has:
    - `role`, the usual shift: a `ChoiceField` of the template ids, required.
    - `works_on`: `TypedMultipleChoiceField(coerce=int, choices=DAY_CHOICES, widget=CheckboxSelectMultiple)`.
  - `RegularDayForm(forms.Form)` holds the per-day overrides:
    - `template`: a `ChoiceField`. `""` means "Same as usual"; the other values are the template ids. A day not in `works_on` is Off, whatever is chosen here.
    - `alt_template`: optional, labelled "or also".
    - `start` and `end`: optional `TimeField`s.
    - `day_earliest_start` and `day_latest_finish`: optional `TimeField`s.
    - `day_latest_finish_next_day`: a `BooleanField`.
  - Build the day formset with `formset_factory(RegularDayForm, extra=0)` (7 forms).
  - The view combines both forms into 7 `DayShift`s. "Same as usual" becomes the role, a day missing from `works_on` becomes Off, and blank times stay blank (S17). The view then runs `validate_regular_shift` and passes `role_template_id` to `save_regular_shift`.
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
  - **Step 1, "Usual shift":** four large choice cards (Morning / Midday / Evening / Float). Each shows its band hint; Float reads "Any shift — goes where the day needs him".
  - **Step 2, "Works on":** seven day toggles.
  - **Step 3, "Any day different?":** one compact row per working day.
    - The row shows the day's label and an evidence line ("Worked 6 of the last 8 Mondays · usual 4:35 AM – 3:35 PM", or "Not a regular day").
    - A "Change" disclosure opens the overrides: shape, "or also", times, "Not before", and "Done by" with "(next day)".
    - Times are optional. The placeholder shows the shape's usual times (`band_fill`, passed as JSON).
  - A Confirm button submits the page. The page must work without JS (`<details>` for the disclosure).
  - Prefill comes from `current_days` and `shift_role` when the driver is confirmed. Otherwise it comes from the suggestion and `suggest_role`.
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
  - `test_editor_blank_times_mean_usual`
  - `test_editor_role_and_days`: a Float role on Mon/Tue/Wed saves the role and three Float days; every other day is Off.
  - `test_editor_thursday_done_by_3pm`: a Morning driver with Thursday's `day_latest_finish` set to 15:00 saves, and the profile shows "· done by 3 PM".
  - `test_editor_morning_or_evening_day`
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
  - Four cards: Morning, Midday, Evening and Float. Each shows the band fields, "Longest shift (hours)", the notes, and "used by N drivers" (as their usual shift) linking to the list.
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

### Task 9: Driver knowledge — strengths, habits, languages, areas (tags)

**Files:**
- **`drivers/models.py`**
  - **`DriverTag`**
    - `name`: CharField(60), unique
    - `category`: choices `strength|habit|language|area`
    - `polarity`: choices `positive|caution`, default `positive`
    - `description`: CharField(200), blank
    - `is_active`: Bool, default True
    - `sort_order`: PositiveSmallIntegerField, default 0
    - `created_by`: FK User, null, blank, SET_NULL, `related_name="+"`
    - `Meta.ordering = ["category", "sort_order", "name"]`
  - **`DriverTagAssignment`**
    - `driver`: FK, `related_name="tag_assignments"`, CASCADE
    - `tag`: FK, PROTECT, `related_name="assignments"`
    - `note`: CharField(200), blank
    - `added_by`: FK User, null, blank, SET_NULL, `related_name="+"`
    - `added_at`: auto_now_add
    - `unique_together = ("driver", "tag")`
- **Migrations:** `drivers/migrations/0066_driver_tags.py` (schema) and `0067_seed_driver_tags.py` (data, reversible). Seed:

| category | polarity | names (in sort order) |
|---|---|---|
| strength | positive | Airport pro · Cruise port pro · VIP & corporate · Large groups · Car seats & families · Long-distance trips · Calm under pressure · Great guest reviews |
| habit | positive | Always early · Taps every status · Picks up extra shifts · Keeps the car spotless |
| habit | caution | Runs late · Slow with luggage · Misses status taps · Hard to reach by phone · Prefers no late nights |
| language | positive | Spanish · Portuguese · French · Haitian Creole · Arabic |
| area | positive | Disney · Universal · Port Canaveral · Downtown Orlando · Tampa |

- **`drivers/driver_knowledge.py` (new)**:
  - `tags_by_category(driver) -> list[tuple[str, list[DriverTagAssignment]]]`, ordered strength, habit, language, area, with labels "Strengths", "Habits", "Languages", "Knows the area". Uses one query (`select_related("tag", "added_by")`).
  - `add_tag(driver, tag, note, user) -> DriverTagAssignment`. Idempotent: if the driver already has the tag, it updates the note.
  - `remove_tag(driver, tag) -> None`
  - `create_tag(name, category, polarity, user) -> DriverTag`. Uniqueness is case-insensitive; a clash raises `ValueError("That tag already exists.")`.
- **`drivers/driver_knowledge_views.py` (new)**. Each view redirects back to `driver_profile` with a `messages` line; no JSON.
  - `driver_tag_add(request, driver_id)`: POST, staff.
  - `driver_tag_remove(request, driver_id, tag_id)`: POST, managers; 403 otherwise.
  - `driver_tag_create(request, driver_id)`: POST, managers. Creates the tag, then assigns it.
- **`drivers/urls.py`**:
  - `<int:driver_id>/tags/add/` → `driver_tag_add`
  - `<int:driver_id>/tags/<int:tag_id>/remove/` → `driver_tag_remove`
  - `<int:driver_id>/tags/new/` → `driver_tag_create`
- **`drivers/templates/drivers/_driver_tags_card.html`**: the "Strengths & habits" card.
  - Chips are grouped by category. Positive chips use the gold accent; caution chips are amber.
  - Each chip shows its note in small text. A tooltip shows who added it and when.
  - The add form is a `<select>` of active, not-yet-assigned tags with `<optgroup>`s per category, an optional note, and an Add button.
  - Managers also see a remove "×" on each chip and a "New tag" mini-form (name, category, polarity).
  - The forms are standalone. **Never nest them inside the profile edit form.**
- **`driver_profile.html` / `drivers/views.py`**: include the card in both modes, under Shift facts. Add `tag_groups` and `available_tags` to the context.
- **Release note:** create `docs/release-notes/2026-10-04-driver-strengths-habits-and-log.md` (audience Dispatchers). Draft:

> Hey team — driver profiles now hold what we know about each driver.
>
> 1. **Strengths & habits:** tag a driver as an airport pro, great with car seats, always early, slow with luggage, the languages they speak and the areas they know — add a short note if it helps.
> 2. **The log:** write down compliments, complaints, incidents and notes, with the date and the trip. Managers can mark a complaint or incident as a strike; the profile shows strikes from the last 12 months.
>
> Anyone on the desk can add tags and log entries; only managers can mark strikes, remove tags, or edit and delete entries.
>
> Drivers never see any of this, and nothing about assigning trips changes.

  (The log ships in Task 10. Keep the note's log line in from the start, because both tasks ship together on this branch.)
- **Test:** `drivers/tests_driver_knowledge.py` (new):
  - `test_seeded_tags`: counts per category; "Airport pro" is a positive strength.
  - `test_dispatcher_adds_tag_with_note`
  - `test_add_twice_updates_note`
  - `test_dispatcher_cannot_remove` (403)
  - `test_manager_removes`
  - `test_manager_creates_tag_case_insensitive_unique`
  - `test_dispatcher_cannot_create_tag` (403)
  - `test_card_groups_and_caution_style`
  - `test_inactive_tag_not_offered_but_still_shown`
  - `test_tags_never_on_driver_app`: log in as the driver, GET the driver-portal pages (index, my details, completed trips); no tag name or note appears.
- [ ] Steps:
  1. Write the failing tests.
  2. Run them and see them fail.
  3. Implement.
  4. Run `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers` and see it pass.
  5. Run `python manage.py makemigrations --check --dry-run`.
  6. Commit, including the release note. Subject: "Driver profiles hold strengths, habits, languages and the areas each driver knows".

---

### Task 10: Driver knowledge — the log (compliments, complaints, incidents, notes) and strikes

**Files:**
- **`drivers/models.py`: `DriverLogEntry`**
  - Fields:
    - `driver`: FK, CASCADE, `related_name="log_entries"`
    - `occurred_on`: DateField
    - `kind`: choices `compliment|complaint|incident|note`
    - `is_strike`: Bool, default False
    - `severity`: choices `""|minor|serious`, blank
    - `leg`: FK `reservations.Leg`, null, blank, SET_NULL, `related_name="+"`
    - `summary`: CharField(140)
    - `details`: TextField, blank
    - `logged_by` and `updated_by`: FK User, null, blank, SET_NULL, `related_name="+"`
    - `logged_at`: auto_now_add
    - `updated_at`: auto_now
  - `Meta.ordering = ["-occurred_on", "-logged_at"]`.
  - Constant: `STRIKE_WINDOW_DAYS = 365`.
- **`drivers/migrations/0068_driver_log.py`**
- **`drivers/driver_knowledge.py`** additions:
  - `log_entries(driver, kind=None) -> QuerySet`
  - `strike_count(driver, today: date) -> int`: strikes with `occurred_on > today − 365 days`.
  - `recent_legs_for(driver, today, days=60) -> QuerySet[Leg]`: the driver's legs from the last 60 days that are not cancelled, newest first.
- **`drivers/forms.py`: `DriverLogEntryForm(ModelForm)`**
  - Fields: `kind`, `occurred_on` (date input, default today), `severity`, `leg` labelled "Trip (optional)", `summary`, `details`, `is_strike`.
  - `__init__(driver, user, ...)`:
    - Limits `leg` to `recent_legs_for(driver)` plus the current value. Each option reads like "Oct 3 · 5:00 AM · MCO → Polynesian".
    - Removes `is_strike` for non-managers.
  - `clean()` messages:
    - `"That date is in the future."`
    - `"A strike must be a complaint or an incident."`
- **`drivers/driver_knowledge_views.py`**:
  - `driver_log_add(request, driver_id)`: POST, staff. A non-manager can never set a strike, even by posting the field.
  - `driver_log_edit(request, driver_id, entry_id)`: GET/POST, managers.
  - `driver_log_delete(request, driver_id, entry_id)`: POST, managers. Confirmed in the UI.
- **`drivers/urls.py`**:
  - `<int:driver_id>/log/add/` → `driver_log_add`
  - `<int:driver_id>/log/<int:entry_id>/edit/` → `driver_log_edit`
  - `<int:driver_id>/log/<int:entry_id>/delete/` → `driver_log_delete`
- **`drivers/templates/drivers/_driver_log_card.html`**: the "Log" card.
  - Header: a strike pill reading "N strike(s) in the last 12 months". It is amber for 1–2 and red for 3 or more, and hidden at 0.
  - Filter links: All / Compliments / Complaints / Incidents / Notes, using `?log=<kind>` and filtered on the server.
  - A timeline, newest first. Each entry shows the date, a kind badge, a strike badge, the summary, the details in a `<details>`, the trip (linked to its reservation if a URL name exists), and "logged by X".
  - "Add to the log" sits in a `<details>` form.
  - Managers get Edit and Delete links.
- **`drivers/templates/drivers/driver_log_edit.html`**: the edit page.
- **Profile hero:** the strike pill next to the name when the count is above 0.
- **Context:** `log_entries`, `log_filter`, `strike_count`, `log_form`.
- **Release note:** update the knowledge note if needed. Trailer as in Global Constraints.
- **Test:** extend `drivers/tests_driver_knowledge.py`:
  - `test_dispatcher_logs_compliment_with_trip`
  - `test_dispatcher_cannot_mark_strike`: posting `is_strike=on` as a dispatcher saves the entry with `is_strike=False`.
  - `test_manager_marks_strike_counted_for_12_months`: a strike 400 days ago does not count.
  - `test_strike_must_be_complaint_or_incident`
  - `test_future_date_refused`
  - `test_trip_picker_only_this_drivers_recent_trips`
  - `test_filter_by_kind`
  - `test_manager_edits_and_deletes`
  - `test_dispatcher_cannot_edit_or_delete` (403)
  - `test_log_never_on_driver_app`
  - `test_hero_shows_strike_pill`
- [ ] Steps:
  1. Write the failing tests.
  2. Run them and see them fail.
  3. Implement.
  4. Run `ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers` and see it pass.
  5. Run `makemigrations --check`.
  6. Commit. Subject: "Driver profiles keep a dated log of compliments, complaints and incidents, and managers can mark strikes".

---

### Task 11: Whole-branch verification (controller)

- [ ] **Full suite.** Run `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching drivers` serially (no `--parallel`; see Task 5, Step 4). Record N tests and the failures. Each failure must be either a known environment failure or shown to be flaky by 3 reruns.
- [ ] **Parity gate.** Re-run it (Task 5, Step 5) on the final commit and record `differences : 0`.
- [ ] **Switch-on smoke test.** Use a throwaway migrated copy of the snapshot.
  - Confirm three drivers (Morning, Midday, Evening) and turn the switch on.
  - Preview Auto-Assign for one busy date.
  - Record:
    - the evening driver keeps a late job;
    - no regular window rejects every pickup;
    - every regular driver's planned base→base is ≤ 12h;
    - the count of 00:00–02:59 legs (S14).
- [ ] **Browser pass.** Cover:
  - the profile: Shift facts, Strengths & habits, the Log, the strike pill, and edit mode
  - Regular Shifts
  - the editor: confirm a Morning driver with a "done by 3 PM" Thursday, and a Float driver
  - the switch
  - Shift Templates

  Check desktop and 375px, with screenshots.
- [ ] **Migrations.** `python manage.py makemigrations --check --dry-run` must print "No changes detected".
- [ ] **Release notes.** Re-read both against `docs/release-notes/README.md`: under 150 words, no field names, and each says what did not change.
