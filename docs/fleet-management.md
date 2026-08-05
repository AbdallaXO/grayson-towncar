# Fleet Management

Vehicle condition, mileage, maintenance and compliance for the in-house fleet,
backed by Samsara telematics.

**Status:** Phase 1 shipped 2026-08-05, running against the live Samsara account
locally. Not yet deployed — see [Before you deploy](#before-you-deploy).

**Pages:** `/dispatching/fleet/` (list) · `/dispatching/fleet/<pk>/` (detail)
Nav: Analytics dropdown → Fleet.

---

## The one-paragraph version

Samsara is a **sensor**, our DB is the **system of record**. A background poller
writes telemetry onto `FleetVehicle` every 3 minutes; a nightly pass rolls it
into per-day mileage rows and refreshes vehicle identity. The pages read only
our own tables — they never call Samsara — and everything a human owns
(intervals, service records, compliance dates, transponders) is edited on the
page, not in the Django admin.

---

## Before you deploy

1. **Set `SAMSARA_API_TOKEN` on Railway.** The name matters — `settings.py:269`
   reads `SAMSARA_API_TOKEN`. `.env` previously called it `SAMSARA_API_KEY`,
   which is why the integration was silently dead for ~25 days (every mapped
   vehicle frozen at 2026-07-11 and nothing noticed). The feed-health tile on
   the fleet list exists so that can't happen quietly again.
2. **Merging to `main` auto-migrates production.** `railway.json` has
   `preDeployCommand: python3 manage.py migrate --noinput`, so migrations 0045
   and 0046 apply on deploy. Both are additive (new nullable columns + new
   tables + a partial unique constraint on `samsara_vehicle_id`, verified to
   have no duplicates), but know that it happens.
3. **Seed something.** The maintenance layer is inert until each active vehicle
   has at least an oil interval. Fleet detail → Maintenance schedule → Add
   interval. Compliance dates and transponder numbers are also worth 20 minutes
   of data entry — the module is only as useful as what's in it.

---

## What's measured, not assumed

Everything below came from `manage.py fleet_probe` against the live account on
2026-08-05. **Re-run it before changing what we request** — guessing produced
two real bugs already.

| Stat type | Coverage | Notes |
|---|---|---|
| `gps` | 11/11 | position, speed, reverse-geo (pre-existing) |
| `obdOdometerMeters` | 11/11 | mileage primary — exact, off the OBD bus |
| `gpsDistanceMeters` | 11/11 | mileage fallback |
| `batteryMilliVolts` | 11/11 | readiness |
| `faultCodes` | 11/11 | readiness |
| `fuelPercents` | 11/11 | readiness — **response key is `fuelPercent`** |
| `engineStates` | 11/11 | On/Off/Idle — **response key is `engineState`** |
| `obdEngineSeconds` | 5/11 | engine hours; display only, nothing built on it |
| `gpsOdometerMeters` | 5/11 | **never used** — settable/drifting, not truth |

### Two traps that cost real bugs

**1. Samsara does not always echo the requested type name back.** You ask for
`fuelPercents`; `/fleet/vehicles/stats` returns the key as `fuelPercent`
(singular). Reading the requested name finds nothing, which looks exactly like
"the plan doesn't include fuel" — while the Samsara dashboard shows fuel fine.
Only `fuelPercents` and `engineStates` do this. Handled by `_STAT_KEY_ALIASES`
in `samsara_service.py`; there's a test that fails loudly if a future type is
added whose response key differs.

The endpoints also disagree on **shape**: `/stats` returns one `{time, value}`
dict, `/stats/feed` and `/stats/history` return a *list*. `_stat_block`
normalises both.

**2. `faultCodes.obdii.diagnosticTroubleCodes[]` is a list of ECUs, not faults.**
Each entry holds its own `confirmedDtcs` / `pendingDtcs` / `permanentDtcs` plus
a `milStatus`. A healthy Suburban returns four empty entries — counting entries
reported "4 faults" on a clean car. We count `confirmed + permanent` inside each
entry, skip `pending` (unconfirmed; would badge healthy cars), and treat a lit
check-engine lamp with no readable code as one fault. `j1939` nests differently:
its entries *are* faults.

### API limits and entitlements

- **Max 4 stat types per request.** `400 "Vehicle stats are currently restricted
  to 4 types."` We request 7, so `_apply_extended_stats` chunks them. Adding an
  8th type costs another request, not a 400.
- `/fleet/vehicles/stats/feed` and `/fleet/vehicles/stats/history` — **entitled**.
  History means a mileage backfill is possible (see [Not built yet](#not-built-yet)).
- `/maintenance/service-tasks`, `/maintenance/work-orders` — **403, not
  licensed.** Samsara sells a Maintenance module; this plan doesn't include it.
  See [Why not two-way](#why-not-two-way-with-samsara-maintenance).
- `/fleet/maintenance/list` — **404, does not exist.** (The abandoned `samsara`
  branch's client was written against this path.)
- `/fleet/dvirs` — POST only (submit endpoint; reading DVIRs needs the driver app).

---

## Architecture

### Where things live

| File | Role |
|---|---|
| `dispatching/mileage.py` | **Pure** OBD→GPS mileage resolver. No DB, no clock, no HTTP. |
| `dispatching/fleet_sync.py` | Daily accrual, master refresh, nightly gate, feed health |
| `dispatching/fleet_health.py` | Readiness chips, service-due findings. **Pure.** Advisory only. |
| `dispatching/fleet_views.py` | Both pages + the JSON edit endpoints |
| `dispatching/samsara_service.py` | API client (pre-existing; extended with `parse_stats_record`) |
| `dispatching/samsara_scheduler.py` | The 3-minute poller (pre-existing; extended) |
| `drivers/models.py` | `FleetVehicle` + the 5 new fleet models |

All five new models live in `drivers/models.py` rather than a new app: they hang
off `FleetVehicle`, and one app keeps the migration collision surface to one
file. **Do not resurrect `samsara_integration/`** — it's orphaned `.pyc` from an
unmerged `origin/samsara` branch whose migration depends on a `drivers/0025`
that never existed on main.

### Models (migrations 0045, 0046)

- **`FleetVehicle`** (extended) — `vin`, `license_plate`, `samsara_name`,
  `transponder_number`/`transponder_type`, four compliance dates, and the
  poller-written telemetry block. Plus a **partial unique constraint** on
  `samsara_vehicle_id` excluding blank: a duplicate ID silently maps two cars to
  one feed and the poller's `{id: vehicle}` dict drops one.
- **`VehicleDayReading`** — one row per vehicle per **local** day. Unique on
  `(vehicle, date)`.
- **`VehicleServiceSchedule`** — recurring interval, miles and/or days.
- **`VehicleServiceRecord`** — a service that happened, with an optional
  out-of-service window.
- **`VehicleFault`** — open fault *episodes*, partial-unique on unresolved rows
  so a fault seen on 1,000 polls is one row.
- **`FleetSyncState`** — per-feed health (and a cursor field, unused until/unless
  the delta feed is adopted).

### Jobs

Everything runs inside the **existing** 3-minute Samsara poller thread under
advisory lock `737_202`. There is no cron in this repo — no Procfile, no Railway
cron, no Celery (`django_celery_beat` is installed but nothing imports celery).

- **Every cycle:** `sync_vehicles()` (GPS in its own call, then extended types
  chunked) → `accrue_vehicle_day()` → feed-health stamp.
- **Nightly:** `should_reconcile()` gates on local hour 3–6am **plus** a
  DB-persisted stamp. The stamp is in the DB on purpose — the one existing
  "run less often" mechanism in this repo (`ghl_integration`'s in-memory
  `_cycle_count % N`) resets on every worker recycle, and `--max-requests 1500`
  makes those routine.
- Manual: `manage.py fleet_reconcile [--dry-run|--accrue-only]`,
  `manage.py fleet_probe [--raw]`.

Nothing in the fleet path may raise: the same leader thread runs the ETA sweep
the dispatch board depends on, and `restartPolicyMaxRetries: 10` means a crash
loop can burn the restart budget and take the web service down.

---

## Rules that are load-bearing

These aren't style preferences — each one exists because of a specific failure.

**NULL is not zero.** `miles_driven = NULL` means unknown and renders as an
em-dash. Zero means the car provably didn't move. Conflating them makes a dead
gateway look like a parked car and poisons every total above it. Any aggregate
states its coverage ("across 26 of 31 days").

**Mileage math lives in exactly one module.** `dispatching/mileage.py`, same
precedent as `pickup_policy.py`. It never emits a negative delta, discards
implausible steps (>900 mi/day), falls back to GPS on a backwards OBD reading,
and **refuses to diff across two different `samsara_vehicle_id` values** — one
gateway moved between cars would otherwise produce a fictional six-figure day.
32 tests, written before it was wired to anything.

**Days are contiguous.** A day's mileage is measured against the *previous*
day's closing odometer, not its own first sample — otherwise every mile driven
between the last poll of one day and the first of the next vanishes, and an
overnight MCO run is exactly that shape.

**`miles_driven` is derived, never accumulated.** The nightly recomputes it from
the stored start/end every run, so a re-run reproduces the row exactly. There is
no `miles += delta` anywhere; an accumulator can't be repaired once it drifts.

**An absent reading never nulls a stored value.** `parse_stats_record` emits only
keys present in the payload, so a GPS-only gateway leaves other columns alone.
Stale-but-real beats fresh-and-null; the `*_at` timestamps let the UI age it.

**Readiness is advisory, always.** No chip, fault, or out-of-service window ever
blocks an assignment, removes a unit from a pool, or subtracts capacity. Guard A
— an assignment-time per-vehicle check — was built and deliberately removed for
firing false positives off stale data (`feasibility_guards.py:140-144`), and
`day_setup.py:33-36` records the founder ruling that "there is no such thing as
a car not working today". **There is deliberately no vehicle status enum**: a
field containing `out_of_service` would eventually get imported by something
that subtracts capacity.

**The pages are DB-only.** No view calls Samsara. `reservations/middleware.py`
sets a 30s Postgres `statement_timeout` on web requests and gunicorn runs
`--timeout 60`; a synchronous external call in a render path already caused one
worker-timeout incident. List page: 9 queries, ~8ms warm, flat regardless of row
count.

**Never mass-resolve faults on a failed API call.** An empty response because
Samsara 500'd is indistinguishable from "all faults cleared" unless you check
status first.

---

## Editing (no Django admin)

By explicit request, the whole fleet job is done on the page. JSON POST
endpoints in `fleet_views.py`, staff-only, house shape (`{"success": bool}`):

| Endpoint | What |
|---|---|
| `fleet/<pk>/details/` | compliance dates, notes, transponder |
| `fleet/<pk>/schedule/` | upsert an interval (on `(vehicle, service_type)`) |
| `fleet/schedule/<pk>/delete/` | remove an interval |
| `fleet/<pk>/service/` | log a service |
| `fleet/service/<pk>/delete/` | remove a record |

**Logging a service auto-advances the matching interval's baseline** — log an
oil change at 58,293 and the oil interval resets to next-due 63,293, no second
entry. Forward-only, so back-filling an old receipt can't rewind a newer
service. Deleting a record deliberately does *not* rewind the baseline;
recomputing which remaining record should own it is guesswork.

**What is NOT editable there:** VIN, plate, and every `samsara_*` column. The
poller owns them and would overwrite a hand edit within 3 minutes; a typo'd
odometer would corrupt the next day's delta. The admin registrations for the
derived models are read-only with `has_add_permission = False`.

---

## Why not two-way with Samsara Maintenance

Aside from not being licensed (403), two-way sync of the same mutable record is
the classic drift generator: when both sides edit an interval you need conflict
resolution, last-write-wins timestamps, and a reconciliation story — real
machinery for 14 cars. It also contradicts the goal of one page that does
everything, and would mean paying for a license to duplicate what already works.

Keep the split: **Samsara knows things we can't** (odometer, faults, battery,
position) and feeds them one-way. **We own** intervals, service history and cost.

The one thing that would change this answer is **DVIR** — driver pre-trip
inspections feeding defects in automatically. That needs the Samsara Driver app
in the chauffeurs' hands, which is a driver-app change and needs its own
conversation.

---

## Not built yet

Ordered by value.

1. **Mileage backfill from `/fleet/vehicles/stats/history`** (entitled,
   confirmed). `VehicleDayReading` only accrues forward from ship date, so the
   first days are em-dashes. A backfill command would fill the last 30 days
   immediately. `finalise_previous_day()` already re-derives a window, so this
   is mostly a fetch + upsert. **This is the obvious next task.**
2. **Readiness chips in Day Setup / the in-house assignment modal.** ~30 lines,
   and it puts low battery / open fault / stale GPS where the dispatcher already
   is at 6am, instead of on a page someone has to remember to visit. Advisory
   only — if anyone proposes "block assignment when fuel < 15%", that's Guard A
   again.
3. **VIN auto-mapper.** VIN is now stored for all 13 Samsara vehicles, so
   matching the unmapped ones is cheap. Samsara has 13 vehicles; we map 11.
   "Metris 10" there matches our #10 by VIN; "Vehicle 14" has no counterpart.
   Our #004 and #015 aren't in Samsara at all.
4. **Per-vehicle cost.** `VehicleServiceRecord.cost` exists but nothing
   aggregates it. Natural home is the existing `vehicle_profit_report`.
5. **Fault episodes.** `VehicleFault` is modelled and admin-registered but the
   sync only writes the *count* to `FleetVehicle.samsara_open_fault_count`;
   nothing populates the episode table yet.
6. **Webhook.** Deliberately cut. Samsara webhooks carry alerts/events, not stat
   updates, so the "vehicle updates" half isn't deliverable that way. The signing
   scheme is unverified and there's no HMAC helper in the repo to copy. Revisit
   only alongside a fault-to-downtime workflow.
7. **Idle time / engine-hour deltas.** Cut. Idle isn't a stat type, and
   poll-derived idle has ~3-minute resolution. In a chauffeur operation, idling
   to hold a cabin temperature for a guest on a delayed flight *is* the service.

---

## Testing

```bash
ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_mileage \
    dispatching.tests_fleet dispatching.tests_samsara
```

241 tests. Full suite: 1824 tests, **5 pre-existing errors** unrelated to this
work (3 × missing `pywebpush`, 1 × GHL creds, 1 × a Windows-only `%-d` strftime
bug at `advisor_display.py:265` that works fine on Linux). `tests_overnight_arrival`
can flake under full-suite load with `database table is locked: reservations_leg`
— a documented SQLite race from background email threads, not a regression.

Always run with `ENABLE_DEBUG_TOOLBAR=0`; the local `.env` enables the toolbar
and it breaks endpoint tests with a `djdt` NoReverseMatch.

---

## Current fleet state (2026-08-05)

14 `FleetVehicle` rows, 13 active, 11 mapped to Samsara. Odometers 55k–227k, all
OBD-sourced. One open fault on #008 (transient — cleared on a later poll).
**#11 (Sprinter) has been reading 7.4–11.5V across polls**, well under the
11.8V no-start threshold — worth a physical battery check.
