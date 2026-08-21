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
- `/fleet/vehicles/locations`, `/fleet/vehicles/locations/history`,
  `/fleet/vehicles/locations/feed` — **entitled.** These serve GPS breadcrumbs
  (`{time, latitude, longitude, heading, speed, reverseGeo}`) rather than the
  stat blocks, and they power the right-click **Vehicle route** menu (see
  ["Where is the car?"](#where-is-the-car-on-the-right-click-menu)). `startTime` and
  `endTime` are both **required** — a missing `startTime` is a 400. A future
  window or an unknown vehicle id answers **200 with an empty list**, so "no
  route" and "call failed" stay distinguishable.
- `/fleet/trips` — **404, does not exist** on this API version. Trips would have
  to be derived from the breadcrumb track.
- `/fleet/routes`, `/addresses` — **401, not licensed.** ("Token requires Routes
  read permissions to call this endpoint.")
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
| `dispatching/vehicle_routing.py` | **Pure** Google Maps link + label rules for the right-click menu |
| `dispatching/vehicle_route_views.py` | The two right-click vehicle endpoints (DB-only) |
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
  Migration 0047 adds the out-of-service window
  (`out_of_service_from`/`_until`/`_reason`) and the three pickup permits with
  their expiries — see "Readiness is advisory" below for why one gates and the
  other doesn't. Permits are flat fields rather than a related model (three
  fixed permits, no prefetch on a pool render); a fourth is a migration.
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

**Averages divide by KNOWN days, not calendar days.** `usage_rate()` is where
per-day / per-week utilisation is computed, and the two kinds of blank day are
not the same number: `None` (unknown — dead gateway) is excluded from the sum
*and* the denominator, while `0` (provably parked) counts in the denominator. Get
this backwards and a week of feed outage halves a busy car's apparent rate, which
then pushes its next-service projection out to never. `per_day` is `None`, never
`0`, when nothing is known — an unknown rate must not sort as the least-used car.
The fleet list computes the same figure from a `Sum`/`Count` aggregate (both
already NULL-excluding), so list and detail cannot disagree.

**A projection that can't be trusted isn't offered.** `days_to_cover()` returns
`None` — not a large number — when the rate is unknown or zero, because someone
books a shop day around it. Service projections render as "≈ Sep 14 at this
rate", never as a bare date, and an overdue interval shows its status chip rather
than a fabricated future date.

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

**Readiness is advisory, always — with exactly one exception, added later.**
No chip, fault, service-record window, or permit ever blocks an assignment,
removes a unit from a pool, or subtracts capacity. Guard A — an assignment-time
per-vehicle check — was built and deliberately removed for firing false
positives off stale data (`feasibility_guards.py:140-144`), and
`day_setup.py:33-36` records the founder ruling that "there is no such thing as
a car not working today".

The exception is `FleetVehicle.out_of_service_from/until/reason`, added on the
founder's explicit request so a car on a lift stops being scheduled. It is
allowed to gate **because it is not machine inference**: a human who knows the
car is down sets it by hand, with a reason and a date window. That is a
different class of fact from a fault code, and the Guard A reasoning — stale
telemetry producing false positives — does not reach it.

Three properties keep it from becoming Guard A again, and they are load-bearing:

1. **Date-windowed, not a status flag.** Every surface asks
   `is_out_of_service_on(date)`. A car in the shop this week is untouched on
   next week's board. There is still no vehicle status enum.
2. **Overridable at assignment time.** `update_inhouse_vehicle_assignment`
   answers `409` with `can_override: true`; the planner offers to force it. A
   forgotten flag can never strand a car that came back early.
3. **Visible, not hidden.** The unit stays in the planner pool, greyed with its
   reason, and Day Setup names it in `warnings` rather than quietly coming up a
   unit short.

The bulk paths differ deliberately: `apply_day_setup` refuses the whole batch
(409, no override — an override there would silently apply to every pair in the
payload), and `copy_vehicle_assignments` skips the broken unit and reports it in
`skipped_out_of_service` so one bad car doesn't cost you the day's plan.

**Permits stay advisory.** `permit_mco` / `permit_sanford` /
`permit_port_canaveral` (+ `*_expires_on`) record the per-vehicle pickup decals
Central Florida requires. A missing or expired one produces a named warning in
`check_driver_feasibility` (`permit_warning`) and never blocks: pickup locations
are free text matched by `categorize_location()`, and MCO is most of the
business, so a hard gate would misfire on the busiest lane. An expired permit is
reported as *not held* — a lapsed decal is worth what no decal is worth. Only
the pickup end is checked; any unit may drop at these places.

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

## "Where is the car?" on the right-click menu

Right-clicking a trip on any dispatch board, or a row on the Fleet list, opens
the shared trip menu (`includes/_trip_context_menu.html`). Alongside Mapping and
Flight Tracker it carries **one** vehicle row — an action with a single line of
context, so it reads like the menu's other items:

> 📍 **Route to pickup**
> `#001 · 1000 Floridian Way, Bay Lake`

It opens Google Maps **directions from the assigned car's live coordinates to
the end of that leg the car is still heading for**, which answers "how far out is
he?" in one click. On the Fleet pages there is no job in view, so the row becomes
**Show on map** over a plain pin on the coordinates.

### Which end it routes to

The verb changes with the leg's status, because the dispatcher's question does:

| Leg status | Row | Routes to |
|---|---|---|
| in-progress / confirmed / on-the-way | **Route to pickup** | the pickup — "how far out is he?" |
| **picked-up / on-location** | **Route to drop-off** | the drop-off — "how much longer has he got?" |
| completed / cancelled | **Show on map** | nowhere: a pin on the car, plus a note saying why |

`vehicle_routing.leg_destination(status, pickup, dropoff)` is the whole rule, and
it reads the status sets `ON_TRIP_STATUSES` / `CLOSED_STATUSES` from
`reservations/constants.py` — the same two sets `samsara_risk.choose_active_target`
uses to pick what the live ETA badge measures against. That shared definition is
the point: while they were separate, a board badge reading "18 min to drop-off"
sat directly above a menu offering directions to the pickup he had already made.
On-location counts as aboard for both, because a car standing at the pickup does
not need directions to the pickup.

Three things are deliberately left unsaid, after a first version said all of
them and read as clutter:

| Not shown | Why |
|---|---|
| The destination address | You right-clicked that trip; the menu header already names it. "→ directions to Disney's Grand Floridian Resort & Spa" was the longest line in the menu, restating the thing you clicked. (Pickup *vs* drop-off is one word, and it changes what the row means — so that much is said.) |
| Make and model | The unit number identifies the car. "#001 · CHEVROLET SUBURBAN" only took up width. |
| The age of the fix | A timestamp is worth reading only when it tells you *not* to trust the position. It appears solely when the signal has gone stale — where it also greys the pin and italicises the line. |

| File | Role |
|---|---|
| `dispatching/vehicle_routing.py` | **Pure** link + label rules. No HTTP, no DB, no clock. |
| `dispatching/vehicle_route_views.py` | The two endpoints (`leg_vehicle_route`, `fleet_vehicle_route`) |

Rows opt in with **`data-fleet-vehicle-id`** — deliberately *not*
`data-vehicle-id`, which already means a vehicle **type** on the capacity
planner. A menu querying the wrong id space would look like it worked.

### Rules worth keeping

- **DB-only.** The position comes from the poller's `samsara_*` columns, never
  from an API call in the request path. So the menu is a couple of indexed
  reads: no cache, no timeout risk, no rate limit, and it answers identically
  when Samsara is unreachable.
- **A stale fix never claims motion.** A gateway that goes quiet mid-drive
  leaves `driving` in the column forever; "Moving · 38h ago" is a
  contradiction. The position is still worth opening (grey dot, "Last seen
  here"), the motion is not.
- **A one-ended route is refused.** `maps_directions_url` returns None unless
  both ends resolve, because Google silently turns a missing origin into
  "directions from your current location" — a lie about where the car is. A leg
  with a blank address at the end it is heading for falls back to the pin, and the
  note names which end is missing.
- **The URL carries the full booked address; the label is trimmed to the
  venue.** Google resolves the full one accurately; a 300px menu row can't
  carry it.

### What was built and then cut

A first version pulled GPS breadcrumbs and rendered the car's actual driven
route: a step-by-step drive/stop itinerary, and a whole-window Google Maps link
tracing the path. Both were cut — the operational question is "how far out is
he?", and the rest was decoration. The code went with them rather than being
left dead.

The research does not have to be repeated if it ever comes back:

- `/fleet/vehicles/locations`, `/fleet/vehicles/locations/history`,
  `/fleet/vehicles/locations/feed` — **entitled.** They serve
  `{time, latitude, longitude, heading, speed, reverseGeo}` breadcrumbs, not
  stat blocks.
- `startTime` and `endTime` are both **required** (a missing `startTime` is a
  400). A future window or an unknown vehicle id answers **200 with an empty
  list**, so "no route" and "call failed" stay distinguishable.
- **`speed` on those endpoints is MPH**, not the km/h the bare name suggests —
  verified sample-for-sample against `/stats/history`'s `speedMilesPerHour`
  across 611 matching timestamps.
- **Sampling is not uniform:** ~every 5 seconds while moving, but only **once an
  hour while parked**. Any stop detection must measure the *clock gap between
  samples*, never count stationary points — an overnight park is often three
  points.
- Measured cost for one vehicle (2026-08-19): 30 min → 69 points / 0.7s; 3 h →
  922 / 0.5s; 24 h → 3,415 / 0.6s / 603 KB; 7 d → 32,454 / 4.6s / 5.9 MB. A
  week is real risk against gunicorn's `--timeout 60`; 24h would be the ceiling.
- Google's Maps URL API caps a directions link at 9 waypoints, so a multi-hour
  track (~2,000 points) has to be thinned. Ramer–Douglas–Peucker run as a
  *priority split* spends that budget on corners rather than straight miles.

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
