# Feature Handoff

> **Feature:** Samsara fleet-telematics integration — live vehicle visibility + schedule-aware ETA/late-risk on the dispatch board.
> **Status as of this handoff:** Built and working locally, **UNCOMMITTED**, not yet deployed to prod. See §3.
> **Author context:** Built iteratively in one working session. This doc is written to be read cold.

---

## 1. Feature Summary

The dispatch backend already knows what *should* happen (scheduled pickup, assigned driver, assigned vehicle) but historically could not see what is *actually* happening to the vehicle in the real world. This feature connects **Samsara** (the company's GPS/telematics provider) to the dispatch app so dispatchers can see, per leg, **whether a driver is going to make his pickup on time** — before the guest or the dispatcher finds out the hard way.

It was built in two phases plus a UI component:

- **Phase 1 — Live vehicle visibility:** a background poller pulls each in-house vehicle's GPS from Samsara every ~3 minutes and stores the latest position/movement snapshot on `FleetVehicle`. Surfaced as plain text ("📍 near MCO • 2m ago").
- **Phase 2 — Schedule-aware ETA + late-risk:** a background "sweep" computes, per driver, the drive-time from the vehicle's live position to the relevant next stop, compares it against the (flight-aware) scheduled pickup, and produces a **feasibility verdict** (on track / tight / at risk). This is surfaced as a **self-contained "live-tracking panel"** on each leg row.

The driving question the feature answers: *"Is this trip going to go wrong, and do we have time to fix it?"*

---

## 2. Business Purpose

Grayson Towncar runs airport transfers (MCO ↔ Disney/Universal), Port Canaveral cruise transfers, and resort-to-resort trips. "We are never late" is a core operating promise.

- **Workflow it improves:** the dispatch board (the screen dispatchers watch all day) and the "All Legs" daily list. Instead of mentally tracking which drivers might be running behind, dispatchers get a per-leg verdict.
- **Pain point it solves:** today a dispatcher only learns a driver is behind when the driver calls, the guest complains, or the driver finally taps "on the way." This feature turns that into a *proactive* signal.
- **Who uses it:** dispatchers (primary). Read-only — it never assigns drivers or messages guests.
- **What decision it helps with:** "Do I need to call this driver / reassign / pre-farm this trip?" The amber/red badge tells them which legs to look at.
- **Manual work it reduces:** eyeballing the board + calling drivers to ask "where are you?" The position/ETA is computed automatically.

**Important business reality:** Samsara coverage is **partial and growing** — not every in-house vehicle is onboarded, and **affiliate drivers will never be in Samsara**. The whole feature treats "this leg's vehicle has no Samsara mapping" as the *normal* case and renders **nothing** for it (no grey "unknown" noise). It only ever *adds* visibility.

---

## 3. Current Status

**Mostly complete — working and verified locally, but UNCOMMITTED and not in production.**

| Aspect | State |
|---|---|
| Phase 1 (live position) | ✅ Built; verified live against the real Samsara account (vehicle "009" returned a real Orlando position). |
| Phase 2 (ETA + risk panel) | ✅ Built; verified via 69 automated tests + local board render. |
| Automated tests | ✅ 69 Samsara tests pass; full suite 481 pass (1 pre-existing, unrelated failure — see §14). |
| Committed to git | ❌ **No.** Everything is uncommitted working-tree changes. |
| Deployed to prod (Railway) | ❌ No. |
| Prod env var (`SAMSARA_API_TOKEN`) | ❌ Not set on Railway (token exists in local `.env`). |
| Prod vehicle mapping (FleetVehicle ↔ Samsara) | ❌ Not done. Done only for a handful of vehicles in the **local scrubbed DB** for testing. |
| Migrations created | ✅ 5 migrations (see §8). |
| Migrations run on prod | ❌ No (local only). |

**Honest summary:** the code is in good shape and tested, but it has **never run in production**, the ETA source has a known caveat (§16), and several thresholds are first-pass guesses that need real-world tuning. Treat it as "ready for review + a controlled prod rollout," not "done."

---

## 4. What Was Changed

Grouped by area. All part of the Samsara feature unless noted.

- **Backend (services/logic):** new `SamsaraService` API client; new pure risk/ETA logic module (`samsara_risk.py`); new in-process background poller (`samsara_scheduler.py`).
- **Models / database:** added `samsara_*` fields to `FleetVehicle`; added `dispatch_*` fields to `Leg`. 5 migrations.
- **Views / routes:** no new URLs. Two existing views annotate legs with the live snapshot: `reservation_details` and the dispatch board `index` (driver-card live leg). The poller is started from `DispatchingConfig.ready()`.
- **Templates:** new live-tracking panel component (partial + styles) injected into the board + All Legs leg rows; Phase 1 text line on the reservation detail page; small badge partial kept for the driver card.
- **JavaScript:** none added. (No new JS — deliberately; data is rendered server-side from DB columns.)
- **APIs / integrations:** Samsara REST API (`/fleet/vehicles`, `/fleet/vehicles/stats`); reuses existing Google Distance Matrix helper for drive times.
- **Forms:** none.
- **Admin:** `FleetVehicle` admin exposes `samsara_vehicle_id` (inline-editable) + read-only live-position fields.
- **Tests:** new `dispatching/tests_samsara.py` (69 tests).
- **Settings / env:** `SAMSARA_API_TOKEN`, `SAMSARA_BASE_URL`; also a one-line `DEBUG_TOOLBAR_CONFIG["IS_RUNNING_TESTS"] = False` fix that unblocks `manage.py test` (see §16).
- **Template tags:** new `samsara_tags.py` inclusion tag `{% samsara_tracking_panel leg %}`.
- **Management commands:** new `samsara_sync_vehicles` (manual sync + `--list-mappings`).

---

## 5. Files Changed

> Verified against `git status` / `git diff --stat`.

| File | Purpose of Change | Important Notes |
|---|---|---|
| `dispatching/samsara_service.py` | **NEW.** Samsara REST client. | `SamsaraService` mirrors `AeroAPIService`: reused `requests.Session`, Bearer auth, returns `{status: success/rate_limited/not_found/error}` dicts, never raises. Also `parse_gps_record()` (pure) and `resolve_assigned_fleet_vehicle(leg)`. |
| `dispatching/samsara_scheduler.py` | **NEW.** Background poller + ETA sweep. | In-process daemon thread, 3-min interval, Postgres advisory lock `737202` (GHL uses `737201`). `sync_vehicles()` writes GPS snapshot + tracks `samsara_stationary_since`; `sweep_eta(now=None)` writes per-leg ETA/risk. Never raises. |
| `dispatching/samsara_risk.py` | **NEW.** Pure ETA/risk/feasibility logic. | `effective_pickup_dt`, `choose_active_target`, `evaluate(…, eta_override=None)`, `evaluate_driver` (chain), `build_panel_context` (panel display state). Only external dep is the cached `get_drive_time`. |
| `dispatching/templatetags/samsara_tags.py` | **NEW.** `{% samsara_tracking_panel leg %}` inclusion tag. | Component's logic lives here/`samsara_risk`, not in the row template. |
| `dispatching/templates/dispatching/includes/_samsara_tracking_panel.html` | **NEW.** Panel markup (3-line card). | Renders nothing when `build_panel_context` returns `None`. |
| `dispatching/templates/dispatching/includes/_samsara_panel_styles.html` | **NEW.** One-time CSS + Montserrat font. | `.stp-*` classes; parchment card, colored left border. Included once per page. |
| `dispatching/templates/dispatching/includes/_leg_dispatch_eta.html` | **NEW.** Small badge partial. | Now used **only by the driver card** on the board; driven by the sweep's `dispatch_risk_status` (different from the panel logic — see §16). |
| `dispatching/management/commands/samsara_sync_vehicles.py` | **NEW.** Manual sync + mapping helper. | Default = one sync+sweep cycle. `--list-mappings` prints FleetVehicles vs live Samsara vehicles. |
| `dispatching/tests_samsara.py` | **NEW.** 69 tests. | Service shapes, parse, sync, sweep, freshness helpers, panel states, chain, render. |
| `drivers/migrations/0035_fleetvehicle_samsara_last_latitude_and_more.py` | **NEW.** Phase 1 FleetVehicle fields. | |
| `drivers/migrations/0036_fleetvehicle_samsara_stationary_since.py` | **NEW.** Stationary-since field. | |
| `reservations/migrations/0111_historicalleg_dispatch_eta_evaluated_at_and_more.py` | **NEW.** Phase 2 Leg ETA/risk fields. | Includes `historicalleg` (simple_history). |
| `reservations/migrations/0112_historicalleg_dispatch_is_moving_and_more.py` | **NEW.** Movement-snapshot Leg fields. | |
| `reservations/migrations/0113_historicalleg_dispatch_vehicle_label_and_more.py` | **NEW.** Vehicle-label Leg field. | |
| `drivers/models.py` | **MOD.** `FleetVehicle` + samsara fields/helpers. | `samsara_enabled`, `samsara_is_fresh` (`SAMSARA_FRESH_MINUTES=15`), `samsara_age_display()`. |
| `reservations/models.py` | **MOD.** `Leg` + dispatch fields/helper. | `dispatch_eta_is_fresh` (`DISPATCH_ETA_FRESH_MIN=10`). |
| `dispatching/views.py` | **MOD.** Annotate legs with snapshot. | `reservation_details` batches the per-leg assigned FleetVehicle; `index` attaches each driver's live "active leg" to its card row (`row["live_leg"]`). No new queries-per-leg (batched). |
| `dispatching/apps.py` | **MOD.** Start poller in `ready()`. | Same guard as `ghl_integration/apps.py` (skip mgmt commands, runserver-parent). |
| `drivers/admin.py` | **MOD.** `FleetVehicleAdmin` exposes `samsara_vehicle_id`. | Inline-editable; live fields read-only. |
| `dispatching/templates/dispatching/legs_filter.html` | **MOD.** Board: panel on leg rows + driver card + load tag/styles. | Desktop + mobile leg-row spots. |
| `dispatching/templates/dispatching/legs_list.html` | **MOD.** All Legs: panel on leg rows. | Desktop + mobile. |
| `dispatching/templates/dispatching/reservation_view.html` | **MOD.** Phase 1 per-leg "Last position" line. | Reservation detail page. |
| `business/settings.py` | **MOD.** `SAMSARA_API_TOKEN`, `SAMSARA_BASE_URL`, debug-toolbar test fix. | See §13, §16. |

**NOT part of this feature (pre-existing/unrelated working-tree noise — do not attribute to Samsara):** deletions of `.dispatch_alerts_sent.json`, `FULL-AUDIT-REPORT.md`, `LEAD_AUTOMATION_STATUS.md`; untracked `.claude/scheduled_tasks.lock`, `docs/flight-arrival-analytics/`, `docs/staff-metrics-comparison-plan.md`.

---

## 6. New Files Created

See §5 for the full list. In short:
- **3 Python logic/service modules** (`samsara_service.py`, `samsara_scheduler.py`, `samsara_risk.py`) — keep the integration self-contained and the heavy work in the background.
- **1 template tag module** + **3 template partials** — the panel is a reusable component (`{% samsara_tracking_panel leg %}`) so the leg-row templates stay clean.
- **1 management command** — manual sync + the incremental vehicle-mapping helper.
- **1 test module** (69 tests).
- **5 migrations** — additive, all nullable (see §8).

---

## 7. Existing Files Modified

- **`drivers/models.py`** — added 8 `samsara_*` fields to `FleetVehicle` plus 3 helpers (`samsara_enabled`, `samsara_is_fresh`, `samsara_age_display`).
- **`reservations/models.py`** — added 9 `dispatch_*` fields to `Leg` plus the `dispatch_eta_is_fresh` property.
- **`dispatching/views.py`** — `reservation_details` resolves each leg's assigned FleetVehicle in one batched query and pins it as `leg.samsara_vehicle`; the board `index` view attaches each driver's badge-carrying "live leg" to its card row. No render-time API calls.
- **`dispatching/apps.py`** — starts the Samsara poller from `ready()`.
- **`drivers/admin.py`** — `FleetVehicleAdmin` now shows/edits `samsara_vehicle_id` and shows the live snapshot read-only.
- **`legs_filter.html` / `legs_list.html`** — load `samsara_tags` + the styles partial once per page, and render `{% samsara_tracking_panel leg %}` on each leg row (desktop + mobile). The old driver-app `live_eta` line is kept but gated so it doesn't double up.
- **`reservation_view.html`** — Phase 1 "Last position" text line under the per-leg vehicle.
- **`business/settings.py`** — env vars + the debug-toolbar test fix.

---

## 8. Data Model / Database Changes

All new fields are **nullable/blank** so legacy rows, un-onboarded vehicles, and affiliate legs are unaffected. No fields were removed or repurposed. **Backward compatible.**

### `FleetVehicle` (drivers app) — written by the poller
| Field | Type | Purpose |
|---|---|---|
| `samsara_vehicle_id` | CharField(64), indexed | The mapping point. Blank = not onboarded → renders nothing. |
| `samsara_last_latitude` / `samsara_last_longitude` | Decimal(9,6) | Last GPS fix. |
| `samsara_last_location_label` | CharField(128) | Reverse-geocoded label from Samsara (no separate geocode call). |
| `samsara_movement_status` | CharField(32) | `driving` / `idle` (derived from speed). |
| `samsara_last_seen_at` | DateTime, indexed | Timestamp of the GPS sample (Samsara's clock). |
| `samsara_last_synced_at` | DateTime | When we last polled (diagnostic). |
| `samsara_stationary_since` | DateTime | When the vehicle last stopped moving (for dwell / "not moving" detection). |

### `Leg` (reservations app) — written by the ETA sweep
| Field | Type | Purpose |
|---|---|---|
| `dispatch_eta_minutes` | Integer | Drive-time (min) to the relevant target; **chained** for mid-trip drivers. |
| `dispatch_eta_target` | CharField(16) | `pickup` / `dropoff` / `next_pickup`. |
| `dispatch_eta_target_time` | DateTime | The (flight-aware) scheduled time of the target. |
| `dispatch_risk_status` | CharField(16), indexed | Badge band for the **driver card** (`on_time/watch/at_risk/late/unknown`). |
| `dispatch_risk_reason` | CharField(255) | Human-readable reason. |
| `dispatch_eta_evaluated_at` | DateTime | Staleness — panel only renders if evaluated within `DISPATCH_ETA_FRESH_MIN`=10 min. |
| `dispatch_is_moving` | Boolean(null) | Movement snapshot at sweep time. |
| `dispatch_stationary_minutes` | Integer(null) | How long stationary at sweep time. |
| `dispatch_vehicle_label` | CharField(50) | Vehicle # snapshot shown in the panel chip. |

### Migrations
| Migration | Adds |
|---|---|
| `drivers/0035_fleetvehicle_samsara_last_latitude_and_more` | First 7 `FleetVehicle.samsara_*` fields |
| `drivers/0036_fleetvehicle_samsara_stationary_since` | `samsara_stationary_since` |
| `reservations/0111_historicalleg_dispatch_eta_evaluated_at_and_more` | `dispatch_eta_*` + `dispatch_risk_*` |
| `reservations/0112_historicalleg_dispatch_is_moving_and_more` | `dispatch_is_moving`, `dispatch_stationary_minutes` |
| `reservations/0113_historicalleg_dispatch_vehicle_label_and_more` | `dispatch_vehicle_label` |

- **Created:** yes (all 5).
- **Run:** locally yes; **on prod, no.**
- **DB assumptions:** advisory lock works on Postgres (prod). On SQLite (local) the lock helper returns `True` unconditionally (single process). Each migration touches the `historicalleg` shadow table too (django-simple-history) — expected.

---

## 9. Main Logic / Algorithm

### Plain English
1. A background poller asks Samsara, every ~3 minutes, "where is each mapped vehicle?" and saves the answer on `FleetVehicle`.
2. A background "sweep" (same cycle) figures out, for each in-house driver, **the next stop that matters** and **whether he can realistically get there in time**, then saves that verdict on the relevant `Leg`.
3. When a dispatcher loads the board, each leg row just **reads** the saved verdict and shows a small colored panel. No live API calls happen while the page loads.

### "Next stop that matters" + feasibility (the core)
- **Free driver** (not currently on a trip): the next upcoming pickup. ETA = drive-time from the vehicle's live GPS → that pickup.
- **Mid-trip driver** (status `picked-up`/`on-location`): two things are flagged —
  1. his **current drop-off** (informational ETA, no deadline), and
  2. his **next pickup, chained**: `drive(GPS → current dropoff) + DROPOFF_SERVICE_MIN(5) + drive(current dropoff → next pickup)`. This is the realistic time he'll arrive at the next pickup *after finishing what he's doing*.
- **Verdict** = compare arrival vs the scheduled pickup time: `slack = minutes_to_pickup − eta`.

### Panel states (`build_panel_context`, what the dispatcher sees)
- `dropoff` target → silent "~N min to drop-off".
- `slack < 0` and pickup is **upcoming** → **at risk (red)** "~N min late projected" — fires **regardless of whether he's on the way** (it's about feasibility).
- `slack < 0` and pickup is **already past**:
  - driver **on the way** → at risk "~N min late",
  - else **airport arrival** with the flight at the gate and ETA > `PANEL_STAGE_WARN_MIN`(10) → **amber** "Flight landed · vehicle ~N min out",
  - else → **nothing** (treated as stale status; don't cry late for a driver who never started).
- `0 ≤ slack < PANEL_TIGHT_BUFFER_MIN`(10), or vehicle stalled (`dispatch_stationary_minutes ≥ PANEL_DWELL_MIN`(8) within `PANEL_DEPARTURE_WINDOW_MIN`(45) of pickup) → **amber tight** ("N min buffer" or "Vehicle not moving").
- else → **on track** (visually silent, just a green pulse) "Arrives ~N min early".

### Inputs / Outputs
- **Inputs:** Samsara GPS (lat/lng/time/speed/reverseGeo), the driver's legs for today, each leg's flight-aware pickup time, the assigned `FleetVehicle`.
- **Outputs:** the `Leg.dispatch_*` columns; the rendered panel.

### Important business rules / assumptions
- **Flight-aware pickup:** for legs with a controlling flight, the pickup time is the flight's best-available **gate arrival** (`Flight.best_arrival_local()`), not the stored `pickup_time`. **Per the founder, arrivals use gate arrival exactly — no deplaning buffer** (dispatchers eyeball deplaning slack manually).
- **Grace window:** the sweep ignores pickups overdue by more than `PAST_PICKUP_GRACE_MIN`(45) min (stale, handled by the normal status workflow).
- **Partial coverage:** un-onboarded / affiliate vehicles render nothing.
- **No synchronous API in render** (hard rule — a past incident caused worker timeouts when a Google call ran in the request path).

### Edge cases handled
- Completed/cancelled legs → no panel.
- Stale telematics (>15 min) → `unknown` (driver-card badge) / panel stays quiet for pickups.
- Drive-time API failure → `unknown`, never crashes.
- Vehicle not mapped or not fresh → quiet.
- Midnight/date rollover → the sweep keys off `now`'s local date.

---

## 10. User Flow (dispatcher)

1. **Where:** the **Dispatch board** (`/dispatching/`, the "Legs Dashboard") or the **All Legs** page (`/dispatching/legs-list/`). These are the screens dispatchers watch.
2. **What they see:** on each leg row, in the right-hand **Driver & Status** column (below the status dropdown), a small parchment **live-tracking panel** for the driver's next relevant stop — *only* when there's a fresh live read worth showing.
3. **What it shows:** a state pill (At risk / Tight / On track) + a live pulse dot, a bold headline (e.g. "~12 min late projected"), a muted evidence line ("ETA 38 min · pickup in 26 min"), and the vehicle number chip (e.g. `#007`).
4. **What they do:** glance for amber/red. Red = call the driver / consider reassigning / pre-farm. Green/silent = leave it alone.
5. **After:** the panel refreshes on the next page load (the background poller re-evaluates every ~3 min). No buttons to click — it's an at-a-glance signal.

Secondary surfaces: the **reservation detail page** shows a Phase 1 "Last position" text line per leg; the board's **in-house driver cards** show a compact badge for that driver's active leg.

---

## 11. UI / UX Notes

- **New component:** the "live-tracking panel" — a compact parchment card with a colored left border, Montserrat font, three lines (pill+dot / headline / evidence) + a right-aligned vehicle-# chip. States: red (at risk), amber (tight), silent green-pulse (on track). It renders nothing when there's no actionable read (no driver, not onboarded, stale, or "stay quiet" cases).
- **No "Awaiting driver assignment" placeholder** — explicitly removed at the founder's request (it was noise).
- **Palette** matches the existing admin tooling (parchment `#f6f1e7`, amber `#e0a300`, red `#c0392b`).
- **Responsive:** rendered in both the desktop table and mobile card variants of both pages.
- **Possible confusion:** (1) the panel's ETA can differ from Samsara's own Dispatch-widget ETA because they use different routing engines (see §16); (2) the feature produces **more** warnings than the first cut because it now flags not-on-the-way drivers who genuinely can't make it (intended).

---

## 12. API / Integration Notes

- **Service:** Samsara REST API, base `https://api.samsara.com`.
- **Endpoints used:**
  - `GET /fleet/vehicles/stats?types=gps` — live GPS for mapped vehicles (poller).
  - `GET /fleet/vehicles` — vehicle list for the `--list-mappings` helper.
- **Auth:** `Authorization: Bearer <SAMSARA_API_TOKEN>` header, set once on a reused `requests.Session`.
- **Data sent:** vehicle-id filter + `types=gps` query params only. No PII sent.
- **Data received:** per-vehicle `gps` block (latitude, longitude, time, speed, `reverseGeo.formattedLocation`). Cursor pagination handled (`pagination.endCursor`, capped at 25 pages).
- **Failure handling:** `429` → `rate_limited` (+`retry_after`); `404` → `not_found`; `401/403` → auth error; network/JSON errors → `error`. The client **never raises**; the poller logs and continues.
- **Second integration:** **Google Distance Matrix** via the existing `drivers/utils.py:get_drive_time()` (traffic-aware, 2-hour cache) for drive times — called **only in the background sweep**, never at render.
- **Rate limits:** small fleet polled every 3 min is well within Samsara limits (not stress-tested in prod).
- **Security/privacy:** read-only; only in-house vehicle positions; affiliates excluded by design. No customer-facing tracking links in this feature.

---

## 13. Environment Variables / Settings

| Variable | Required? | Purpose | Notes |
|---|---|---|---|
| `SAMSARA_API_TOKEN` | Yes (to activate) | Bearer token for the Samsara API. | In local `.env`. **Not yet on Railway.** When empty, the whole feature is inert (no calls, no DB writes, no UI). |
| `SAMSARA_BASE_URL` | No | Override Samsara API base URL. | Defaults to `https://api.samsara.com`. |
| `GOOGLE_MAPS_API_KEY` | Yes (already present) | Drive-time estimates. | Pre-existing; reused. |
| `REDIS_URL` | No | If set, cache backend is Redis; else LocMemCache. | Pre-existing. Affects the `get_drive_time` cache and cross-worker behavior (see §16/§19). |

Other settings touched: `business/settings.py` adds `DEBUG_TOOLBAR_CONFIG["IS_RUNNING_TESTS"] = False` so `manage.py test` runs (debug toolbar otherwise aborts the test runner — see §16). Poller tunables live in `samsara_scheduler.py` (`INTERVAL_SECONDS=180`, lock `737202`); risk tunables in `samsara_risk.py` (`PAST_PICKUP_GRACE_MIN=45`, `PANEL_TIGHT_BUFFER_MIN=10`, `PANEL_DWELL_MIN=8`, `PANEL_DEPARTURE_WINDOW_MIN=45`, `PANEL_STAGE_WARN_MIN=10`, `DROPOFF_SERVICE_MIN=5`); freshness on the models (`FleetVehicle.SAMSARA_FRESH_MINUTES=15`, `Leg.DISPATCH_ETA_FRESH_MIN=10`).

---

## 14. Testing Done

- **Automated:** `dispatching/tests_samsara.py` — **69 tests, all passing.** Covers: `SamsaraService` status-dict shapes + pagination + never-raises; `parse_gps_record`; `sync_vehicles` (inert without token, populates only mapped); freshness helpers; `choose_active_target` grace logic; `evaluate` risk bands; `evaluate_driver` chain (free vs mid-trip); `build_panel_context` (all states incl. feasibility/overdue/arrival/stalled); panel partial + tag render.
  - Command: `.venv/Scripts/python.exe manage.py test dispatching.tests_samsara`
- **Full suite:** `manage.py test` → **481 pass, 1 error.** The 1 error is **pre-existing and unrelated**: `test_ghl_full.py` (a root-level manual script) crashes at import printing a `✅` emoji to the Windows cp1252 console. Not caused by this feature.
- **Live (Phase 1):** ran `manage.py samsara_sync_vehicles --list-mappings` against the **real Samsara account** — returned the real fleet; mapped one real vehicle and confirmed a real Orlando GPS position flowed into `FleetVehicle`.
- **Local board render:** authenticated Django test-client GET of `/dispatching/` and `/dispatching/legs-list/` returned 200 with the panel markup present for all states (used staged demo data because the scrubbed local DB's legs are time-shifted).

### What was NOT tested
- **Production** — never run on Railway / Postgres / multi-cycle live.
- **Samsara native ETA** — not used (we use Google); the two were observed to disagree (§16).
- The live **"Vehicle not moving" (stalled)** headline end-to-end — proven by unit test, but the three stall conditions never coincided on the live demo data.
- Real **rate-limit / outage** behavior against Samsara (only simulated in tests).
- Load/perf with a full board of mapped vehicles in prod.

---

## 15. How to Test This Feature Again

**Setup**
1. Local env per project `CLAUDE.md`: `DJANGO_DEBUG=1 .venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000 --noreload`, login `localtest` / `Local2026!`.
2. Ensure migrations applied: `.venv/Scripts/python.exe manage.py migrate`.
3. Put `SAMSARA_API_TOKEN` in `.env` (already there locally).

**Automated**
- `.venv/Scripts/python.exe manage.py test dispatching.tests_samsara` → expect **OK (69)**.

**Live data (Phase 1 + mapping)**
- `.venv/Scripts/python.exe manage.py samsara_sync_vehicles --list-mappings` → see real Samsara vehicles next to FleetVehicles.
- Map one in Django admin (`/admin/drivers/fleetvehicle/`) by setting `samsara_vehicle_id`.
- `.venv/Scripts/python.exe manage.py samsara_sync_vehicles` → "Synced N… ETA sweep: flagged N…".

**Board (Phase 2)**
- Visit `/dispatching/?date=<today>` and `/dispatching/legs-list/`. A mapped driver with an upcoming leg should show a panel.
- **Note:** the scrubbed local DB's legs are time-shifted, so to see the full range you may need to stage data (set a leg's `status`/`pickup_time` or a controlling flight's `estimated_gate_arrival_local` to a near-future value), then re-run the sync command.

**Edge cases to check**
- Unmapped/affiliate vehicle → no panel.
- Pickup in 30 min, vehicle ~35 min away, driver NOT on the way → **red** "~5 min late projected".
- Pickup in 30, vehicle ~30 away → **amber tight**.
- Mid-trip driver whose next pickup can't be reached after finishing current drop-off → **red** "… · after current trip".
- Vehicle stale (set `samsara_last_seen_at` 30 min ago) → quiet / grey on driver card.

---

## 16. Known Issues / Bugs

1. **ETA source mismatch (design caveat, not a crash).** `dispatch_eta_minutes` uses **Google Distance Matrix** from the Samsara GPS point, NOT Samsara's own routing. They can differ noticeably (observed Google 18 min vs Samsara's Dispatch widget 28 min). Decision pending (§18). The number shown is Google's.
2. **Driver-card badge vs panel use different logic.** The board **driver card** still uses the small `_leg_dispatch_eta.html` badge driven by the sweep's `dispatch_risk_status` bands (`evaluate`), which is **not** the same as the panel's feasibility logic (`build_panel_context`). They can disagree (e.g. the panel may stay quiet for a not-on-the-way overdue leg while the badge says "late"). Intentional for now, but worth unifying.
3. **More warnings than before.** The feasibility model intentionally flags not-on-the-way drivers who can't make it. If it feels noisy, tune `PANEL_TIGHT_BUFFER_MIN` / `DROPOFF_SERVICE_MIN` / `PANEL_STAGE_WARN_MIN`.
4. **Local data is time-shifted.** The scrubbed dev DB's legs are usually in the past relative to the real clock, so everything reads "late"/quiet unless you stage near-future data. This masks real behavior locally; prod during operating hours is the real test.
5. **`DROPOFF_SERVICE_MIN=5` and the chain drive-times are estimates.** The chain assumes a flat 5-min drop-off service and Google drive-times; not validated against real turn times.
6. **`samsara_stationary_since` accuracy depends on poll cadence.** It's stamped when a poll first sees the vehicle idle, so dwell can be under-counted by up to one interval (~3 min).
7. **Debug-toolbar test hack.** `DEBUG=True` is hardcoded, so `debug_toolbar` is always installed and aborted `manage.py test` (`debug_toolbar.E001`); fixed with `IS_RUNNING_TESTS=False`. Harmless but is a settings change bundled with this feature.
8. **Uncommitted.** Everything is working-tree only — a `git checkout`/stash could wipe it. Commit before doing anything risky.
9. **Multi-worker caveat.** Today prod is a single gunicorn worker. The advisory lock protects the poller if that ever changes, but the `get_drive_time` LocMemCache (when no Redis) is per-process.

---

## 17. Unfinished Work

**Critical before production**
- [ ] Review the code (this handoff is the trigger).
- [ ] Commit the feature (currently uncommitted).
- [ ] Add `SAMSARA_API_TOKEN` to Railway env.
- [ ] Run the 5 migrations on prod.
- [ ] Map prod FleetVehicles → Samsara IDs (via admin / `--list-mappings`), incrementally.
- [ ] Smoke-test on prod during operating hours (real upcoming legs).

**Important but not blocking**
- [ ] Decide + (if chosen) implement Samsara native ETA to reconcile with the Dispatch widget (§16.1, §18).
- [ ] Tune thresholds against 1–2 weeks of real data.
- [ ] Decide whether to unify the driver-card badge with the panel logic (§16.2).

**Nice to have later**
- [ ] Full day-long chain beyond the immediate next pickup (currently one hop ahead).
- [ ] A dedicated "at-risk / live ops" page (one focused list sorted by risk).
- [ ] Push/ntfy alert when a leg first crosses into at-risk.
- [ ] Customer live-tracking links, geofence arrival events, vehicle-readiness (DVIR/fuel) — deferred phases.

---

## 18. Questions / Decisions Needed

1. **ETA source — Google vs Samsara native?** Matters because the panel ETA visibly differs from the Samsara Dispatch widget dispatchers also look at. Google is traffic-aware and already wired; Samsara native would match the widget but needs endpoint/plan confirmation. *Recommend deciding before wide rollout.*
2. **When to commit + deploy?** It's tested but unproven in prod. Decide on a controlled rollout (few mapped vehicles first).
3. **Threshold tuning.** Are the defaults (10-min tight buffer, 5-min drop service, 10-min stage warn, 45-min grace) right for Orlando operations? Needs real data.
4. **Driver-card badge unification** (§16.2) — keep two logics or converge?
5. **Arrival deplaning buffer** — currently *gate arrival exactly* per the founder. Revisit if dispatchers find arrival warnings too eager.

---

## 19. Code Quality Notes

- **Clean overall.** Logic is separated: `samsara_service` (I/O), `samsara_risk` (pure logic, unit-tested), `samsara_scheduler` (orchestration). Mirrors existing patterns (`AeroAPIService`, `ghl_integration` scheduler) for familiarity.
- **Duplication:** the **driver-card badge** vs the **panel** are two display logics over the same data (§16.2) — the main thing to consider refactoring. Some banding wording is repeated between `evaluate` (badge) and `build_panel_context` (panel).
- **Performance:** render path does **no** API/heavy work (reads DB columns); view annotations are batched (no N+1). Drive-time is cached 2h. Background sweep makes ≤2 drive-time calls per busy driver per cycle.
- **Security:** read-only integration, no PII to Samsara, token via env, never logged. Affiliates excluded.
- **Maintainability:** tunables are named module constants; the component is a single inclusion tag. The `historicalleg` migrations add some schema weight (simple_history) but that's the project norm.
- **Watch-outs:** the chain assumes the "next pickup" is the next non-finished leg by pickup time; unusual leg orderings could mis-pick. The stationary tracking is poll-cadence-bound.

---

## 20. Suggested Next Steps (concrete checklist)

1. **Read the code** in this order: `samsara_risk.py` (logic) → `samsara_scheduler.py` (sweep) → `samsara_service.py` (I/O) → `tests_samsara.py` (behavior spec) → the two templates + `samsara_tags.py`.
2. **Run the tests:** `.venv/Scripts/python.exe manage.py test dispatching.tests_samsara`.
3. **Decide the ETA source** (§18.1).
4. **Commit** the feature on a branch (exclude the unrelated tree noise listed in §5).
5. **Deploy to prod:** push → add `SAMSARA_API_TOKEN` on Railway → confirm migrations ran.
6. **Map 2–3 prod vehicles** via `samsara_sync_vehicles --list-mappings` + admin.
7. **Watch the board** during operating hours; sanity-check a few panels against reality.
8. **Tune thresholds** after observing; then map the rest of the onboarded fleet.

---

## 21. Commands / Useful References

```bash
# Run the app (local)
DJANGO_DEBUG=1 .venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000 --noreload
#   login: http://127.0.0.1:8000/users/login/  user: localtest  pass: Local2026!

# Tests
.venv/Scripts/python.exe manage.py test dispatching.tests_samsara      # this feature (69)
.venv/Scripts/python.exe manage.py test                                # full suite

# Migrations
.venv/Scripts/python.exe manage.py makemigrations
.venv/Scripts/python.exe manage.py migrate

# Samsara management command
.venv/Scripts/python.exe manage.py samsara_sync_vehicles                # one sync + ETA sweep
.venv/Scripts/python.exe manage.py samsara_sync_vehicles --list-mappings # map helper

# System check
.venv/Scripts/python.exe manage.py check
```

- **Key files:** `dispatching/samsara_{service,scheduler,risk}.py`, `dispatching/templatetags/samsara_tags.py`, `dispatching/templates/dispatching/includes/_samsara_*.html`, `dispatching/tests_samsara.py`.
- **Background work:** poller starts in `dispatching/apps.py:ready()`; the GHL scheduler is the sibling pattern (`ghl_integration/scheduler.py`, lock `737201`).
- **Logs:** standard `logging.getLogger(__name__)`; look for `"Samsara: synced N vehicle(s)"` and `"Samsara ETA sweep: flagged N leg(s)"`.

---

## 22. Review Request for Another AI/Developer

> Please review this feature handoff (`docs/Samsara_feature_handoff.md`) and the related Samsara code in `dispatching/samsara_service.py`, `dispatching/samsara_scheduler.py`, `dispatching/samsara_risk.py`, `dispatching/templatetags/samsara_tags.py`, the `_samsara_*` / `_leg_dispatch_eta` templates, the `FleetVehicle.samsara_*` and `Leg.dispatch_*` model fields, and `dispatching/tests_samsara.py`.
>
> Look for: bugs, missing edge cases, incorrect feasibility/chain logic (especially mid-trip chaining and the flight-aware arrival handling), security or privacy issues, performance problems (any chance of synchronous third-party calls in the request/render path, or N+1 queries), bad assumptions, race conditions in the background poller/sweep, and anything that should be fixed before production.
>
> Pay special attention to: (1) the ETA-source mismatch between our Google-based ETA and Samsara's own routing; (2) the divergence between the driver-card badge logic (`evaluate`/`dispatch_risk_status`) and the panel logic (`build_panel_context`); (3) the threshold constants in `samsara_risk.py`; (4) behavior when Samsara coverage is partial or the API fails. The feature is currently UNCOMMITTED and has never run in production — call out anything risky for a first prod rollout.
