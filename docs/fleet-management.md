# Fleet Management

Vehicle condition, mileage, maintenance and compliance for the in-house fleet,
backed by Samsara telematics.

**Status:** Phase 1 (vehicle table, telemetry, intervals) shipped 2026-08-05.
Phase 2 — the **Fleet desk** for the first dedicated fleet manager: the downtime
ledger, the demand-aware outlook, reported issues, fault-code episodes, alerts,
the report, and the fleet-manager role — shipped 2026-09-14. See
[Before you deploy](#before-you-deploy) for the two switches it needs.

**Pages:**
`/dispatching/fleet/` (desk) · `/dispatching/fleet/vehicles/` (table) ·
`/dispatching/fleet/outlook/` (demand vs fleet, day by day) ·
`/dispatching/fleet/report/` (management) · `/dispatching/fleet/<pk>/` (one car)
Nav: **Fleet** is a top-level entry for every staff user. A profile flagged
`is_fleet_manager` gets a fleet-only top bar and lands on the desk after login.

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
   has at least an oil interval. The desk's *Setup* fold offers "give every unit
   the standard intervals it's missing" (oil 5,000 mi / 180 d, tyres 7,500 mi,
   brakes 15,000 mi, inspection 365 d — a starting point clocked from today's
   odometer, NOT a manufacturer schedule; correct each car). Compliance dates
   and transponder numbers are also worth 20 minutes of data entry.
4. **Flag the fleet manager.** Django admin → User profiles → tick
   `is_fleet_manager` on their profile and make sure the phone number is set.
   That is what gives them the fleet top bar, the desk as their landing page,
   and the alert texts. Every staff user can still open every fleet page.
5. **Decide on alerts.** `FLEET_ALERTS_ENABLED=true` on Railway switches on the
   two texts (a reported issue the moment it's filed; a morning digest on days
   there is something in the NOW group). Off by default for the same reason the
   wake-up calls are — a dev copy of the DB carries real numbers. Extra
   recipients: `FLEET_NOTIFY_PHONES` (comma-separated). The in-app desk works
   either way.

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
  Migration 0047 added the three pickup permits with their expiries (flat
  fields: three fixed permits, no prefetch on a pool render; a fourth is a
  migration). It also added a single out-of-service window on the row, which
  **migration 0055 removed** in favour of the `VehicleDowntime` ledger below —
  the two open windows at the time were carried across as closed history.
  **Migration 0056 re-added the three columns, nullable and unused**, after a
  Railway rollback on deploy night put pre-ledger code on the new schema and
  every vehicle-loading page failed with `UndefinedColumn`. Nothing reads or
  writes them; they exist so a rollback can boot. Drop them again only when a
  rollback to a pre-ledger build is no longer plausible.
- **`VehicleDowntime`** (0055) — one row per shop visit / breakdown: category,
  reason, shop, `starts_on`, `expected_back_on` (first day BACK), `ended_on`
  (actual, NULL while open), who opened and closed it, and the demand verdict
  saved when it was planned. This is BOTH the live scheduling state
  (`FleetVehicle.is_out_of_service_on` reads it) and the downtime ledger the
  report counts. See [How downtime gates](#how-downtime-gates).
- **`VehicleIssue`** (0055) — a problem a person noticed: title, severity
  (do-not-drive / fix-soon / watch), who reported it, and the resolution when
  closed. The dispatch-to-fleet handoff that used to be a phone call.
- **`UserProfile.is_fleet_manager`** (users 0034) — the role flag.
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

**Readiness is advisory, always — with exactly one exception.**
No chip, fault, service-record window, or permit ever blocks an assignment,
removes a unit from a pool, or subtracts capacity. Guard A — an assignment-time
per-vehicle check — was built and deliberately removed for firing false
positives off stale data (`feasibility_guards.py:140-144`), and
`day_setup.py:33-36` records the founder ruling that "there is no such thing as
a car not working today".

The exception is the **downtime ledger** (`VehicleDowntime`), the successor to
the single out-of-service window. It is allowed to gate **because it is not
machine inference**: a human who knows the car is down says so, by hand, with
a reason and dates. That is a different class of fact from a fault code, and
the Guard A reasoning does not reach it.

### How downtime gates

Three rules, each load-bearing (they are also the docstring on the model):

1. **Blocked from `starts_on` up to — not including — the return.** The return
   is `ended_on` once closed, or `expected_back_on` while open. So a planned
   Tuesday–Wednesday slot releases the car on Thursday's board **by itself**;
   fleet does not have to be at a keyboard at 5 AM for dispatch to have it.
2. **An open row past its expected-back day is a question, not a block.** The
   unit is usable, the pool shows a soft amber *"back? not confirmed"* tag
   (`FleetVehicle.downtime_notice`), the Fleet desk lists it at the top as
   *"expected back Tue — not confirmed"*, and it stays there until someone
   closes the row with the real return date or pushes the expected date out
   (which re-blocks). A forgotten row costs a nag, never a car.
3. **Overridable at assignment time**, exactly as before:
   `update_inhouse_vehicle_assignment` answers `409` with `can_override: true`.
   The desk shows an override as *"#7 is marked down but George has it today"*
   — the strongest hint to close the row.

Every question is per DATE (`is_out_of_service_on(day)`), never "now": the
planner schedules future dates, and a car in the shop this week is a normal car
on next week's board. Pool renders load units through
`FleetVehicle.objects.with_open_downtimes()` so 17 units cost one extra query.

The bulk paths are unchanged: `apply_day_setup` refuses the whole batch (409, no
override), and `copy_vehicle_assignments` skips the broken unit and reports it
in `skipped_out_of_service`. Out of service is **no longer a bulk-edit field** —
a downtime carries a reason, a shop and a return date per car.

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
| `fleet/<pk>/details/` | compliance dates, notes, transponder, permits (out of service is refused here by name — it's a downtime now) |
| `fleet/<pk>/schedule/` | upsert an interval (on `(vehicle, service_type)`) |
| `fleet/schedule/<pk>/delete/` | remove an interval |
| `fleet/<pk>/service/` | log a service |
| `fleet/service/<pk>/delete/` | remove a record |
| `fleet/<pk>/check-window/` (GET) | the demand check: `?from=&back=&ignore=` → clear / tight / conflict per day |
| `fleet/<pk>/downtime/` | open a downtime; `409 needs_ack` when a day is short, saved with `acknowledge: true` |
| `fleet/downtime/<pk>/update/` | move or re-describe an open one (re-judged without its old self) |
| `fleet/downtime/<pk>/close/` | it's back: `ended_on`, optional note, optionally resolves the linked issue |
| `fleet/downtime/<pk>/delete/` | only while it hasn't cost a day; otherwise close it |
| `fleet/<pk>/issue/` | report a problem; `take_down: true` opens a downtime in the same click; texts fleet |
| `fleet/issue/<pk>/resolve/` | close it with what was done |
| `fleet/<pk>/standard-intervals/`, `fleet/standard-intervals/` | add the standard intervals a unit (or every unit) lacks |

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
> 🏁 **Route to drop-off**
> ⤴ **Route via current drop-off**  `NEXT`
> &nbsp;&nbsp;&nbsp;via Orlando International Airport (MCO)
> `#001 · 1000 Floridian Way, Bay Lake`

Each row opens Google Maps **directions from the assigned car's live coordinates**
to that end of the trip, so "how far out is he?" and "how much longer has he got?"
are both one click from the board. On the Fleet pages there is no job in view, so
the block drops to a single **Show on map** over a plain pin.

The third row appears **only while the driver is still running another job** (see
"Through a job in progress" below).

### Both ends, one of them badged

The rows are always **both** ends, in the trip's own order — the same order as
the Copy pickup / Copy drop-off pair further down the menu — so they never swap
places under a dispatcher's cursor as a trip progresses. What moves is the
`next` badge:

| Leg status | Badged `next` |
|---|---|
| in-progress / confirmed / on-the-way | **Route to pickup** — he is still on his way to the guest |
| **picked-up / on-location** | **Route to drop-off** — he has the guest; the open question is how much longer |
| completed / cancelled | neither: nothing is next |

`vehicle_routing.next_stop_kind(status)` is the whole rule, and it reads the
status sets `ON_TRIP_STATUSES` / `CLOSED_STATUSES` from
`reservations/constants.py` — the same two sets `samsara_risk.choose_active_target`
uses to pick what the live ETA badge measures against. That shared definition is
the point: while they were separate, a board badge reading "18 min to drop-off"
sat directly above a menu offering only the pickup he had already made.
On-location counts as aboard for both, because a car standing at the pickup does
not need directions to the pickup.

An end with **no address** is dropped from the list rather than rendered as a
pin, because a pin labelled "Route to drop-off" lies about what the row opens.
The note under the rows names the missing end. Only when *neither* end has an
address does the block fall back to `map_url`, the plain pin.

### Through a job in progress

Two jobs back to back is the case that breaks a straight-line answer. The car is
halfway to MCO with a guest aboard and the next job starts back at Port Orleans:
"how far is the car from Port Orleans" is then a number nobody should act on,
because nobody is driving there next. The menu on **that next job** grows a third
row, `via_dropoff`:

    car's live GPS  →  the drop-off it is still running  →  this leg's pickup

as ONE Google Maps route with the middle stop as a waypoint, so both hops come
back timed. It takes the `next` badge off the plain pickup row, since driving
straight there is not the trip the car is on. Its sub-line names the middle stop
— unlike the two ends, that address belongs to a *different* trip and can't be
read off the card you right-clicked.

`vehicle_route_views._leg_being_run()` finds it: same driver, same day, scheduled
no later than this leg, status in `ON_TRIP_STATUSES`, latest one wins. An "in
progress" job scheduled *after* the one you are looking at is stale status data,
not a job standing in the way, so it is excluded. The chain never appears on a leg
that is itself under way or already closed — nothing stands between a car and the
job it is already running.

The link is driving time only: it does not pad the minutes spent at the drop-off
itself. Google is timing the driving, the dispatcher knows what a hand-off costs,
and the board's own risk badge already adds that allowance (`samsara_risk` chains
the same two hops with `DROPOFF_SERVICE_MIN` between them, which is what puts
"ETA 56 min vs pickup in 48 min" on the card).

Three things are deliberately left unsaid, after a first version said all of
them and read as clutter:

| Not shown | Why |
|---|---|
| The destination addresses | You right-clicked that trip; the menu header already names it. "→ directions to Disney's Grand Floridian Resort & Spa" was the longest line in the menu, restating the thing you clicked. The *words* pickup and drop-off stay, because they are what the row means. |
| Make and model | The unit number identifies the car. "#001 · CHEVROLET SUBURBAN" only took up width. |
| The car, twice | The unit number and last-seen place sit on ONE line under both routes, not repeated inside each row. |
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

## The Fleet desk

`/dispatching/fleet/` — `fleet_views.fleet_desk` → `fleet_desk.load_desk()`
(the only place that queries) → `fleet_attention` (pure) + `fleet_capacity`
(pure arithmetic over loaded legs). One picture for the page, the morning text
and the tests.

What it shows, top to bottom (redesigned 2026-09-14 to the handoff in
`design_handoff_fleet_manager/`), and why nothing else:

- **The state band** — every active unit in exactly one of five states (ready,
  watch, booked in, back-but-unconfirmed, off the road), plus the shop panel:
  bookings held, units down now, or which units have no chauffeur today.
- **Do now** — one row per UNIT with a decision on it, from `fleet_queue.py`
  (pure, same loaded rows as the digest). Five tags only: Needs the shop /
  Watch / Not confirmed back / Booked in / Off the road. Handled rows sink and
  take the settled colour. Fault codes are chips (code + the car's own
  description — there is no plain-English dictionary yet); every row carries
  its action: Find a window, Take off road (same-day downtime), Mark back on
  the road, Cancel the booking.
- **Paperwork** — grouped by document and state ("MCO airport permits — 17 of
  19 units", "Registration, insurance and inspection dates — #17, #18, #19"),
  never one line per car.
- **When can I take a car down?** — the shop-window finder, seven days, see
  [Planning downtime around demand](#planning-downtime-around-demand).

Left out on purpose because nothing in the data backs them: the fault-code
dictionary, a "restricted use" state. The vehicle table is the Vehicles tab,
not repeated on the desk. (The weekly inspection walk was on this list until
2026-09-15 — see [The day](#the-day) and [Inspections](#inspections).)

## The day

`/dispatching/fleet/day/` — `fleet_views.fleet_day` → `fleet_day.load_car_day()`
(the only place that queries) → `fleet_day.build_day()` (pure). One row per
CAR, its trips across a shared clock, the holes between them named.

A leg has no FK to a physical car — `Leg.vehicle` is a `rates.Vehicle`, a
pricing class. The only link is the chauffeur:

    FleetVehicle -> DriverVehicleAssignment(date) -> Driver -> that driver's Legs

`DriverVehicleAssignment` is unique on (driver, date), NOT (vehicle, date), so a
car can carry several chauffeurs in a day (the Day Setup AM/PM share). Every
unit merges all its holders' slots; the seam between two of them is a HANDOFF
and is never offered as shop time.

**The honesty rule, which is the reason the page exists.** An unbuilt day and an
empty car are the same absence of rows, and only one of them means the car is
free — that was bug 2 in the Phase 3 audit. A boolean "is it built" is not
enough either, because the board fills in as a GRADIENT: measured 2026-09-15,
97% of that day's legs carried a chauffeur, 85% the next day, 62% the day after,
0% beyond. So the page leads with its assignment coverage and softens every
empty car below `CONFIDENT_COVERAGE` (90%) to "not assigned yet".

Four states that must never collapse into each other: **not built** (page-level),
**chauffeur but no trips**, **no chauffeur**, **in the shop**.

Slot ends come from `scheduler.estimate_job_end_time` — the same estimator
`fleet_windows.hourly_need` uses, so this page and the shop grid can never
disagree about when a job is over. That estimator is a p75 planning number and
is never used for feasibility anywhere; the same rule applies here. The page
prints no turnaround verdict: labelling a gap "tight" is the feasibility
engine's job. It states the gap's LENGTH and marks only whether a window is long
enough for shop work (90 min + 30 min to get the car there, plus 60 more behind
an airport arrival, whose pickup time moves with the flight).

The axis is derived from the day's own data as DATETIMES, never `.hour`
arithmetic — real days run 04:30–22:30 and a 23:44 pickup clears after midnight.
It is deliberately NOT the 7a–5p shop axis, which would crop both ends silently.

Capped at three days out (`DAY_CHOICES`), because that is as far as the board is
genuinely built.

## Inspections

`/dispatching/fleet/inspections/` — `fleet_views.fleet_inspections` →
`fleet_inspection.load_week()` → `build_week()` (pure). One car, one week, one
walk-around; `VehicleInspection` is unique on (vehicle, week_start).

**The reset is the schema.** `week_start` is the Monday of the ISO week, so
asking about a different week is what empties the round. No job runs, nothing is
cleared down, and last week's record is kept rather than overwritten. "Is the
fleet inspected this week" is one COUNT.

**The checklist lives in one list** at the top of `dispatching/fleet_inspection.py`
in plain English, and can be edited without a migration. Answers are stored by
KEY in a JSONField, so an item later removed still reads back on an old record.
Every item is good / flag / n-a, with an optional note and photos
(`VehicleInspectionPhoto` — a JSON field cannot hold a file). `clean_results`
treats the browser payload as untrusted: unknown keys and invented states are
dropped.

**The suggestion composes `fleet_day`.** A car out on a run all day cannot be
walked, so the daily handful is ordered: no chauffeur today, then the longest
genuinely USABLE window (not the longest raw hole — a three-hour gap behind an
airport arrival is not three hours you can hold a car for), then whoever has
gone longest unseen. `DAILY_TARGET` is 5; 19 active units needs about 4 a day.
A unit in the shop all week is marked as such and is NOT counted as missed.

Finding something files a `VehicleIssue` with source `fleet`. It never takes the
car off the road — only the downtime ledger does that.

Two deviations from the Phase 3 audit, both on the founder's instruction
(2026-09-15): per-item checklists and photos were on its "do not build" list, and
the per-car windows got their own page rather than a column on the Vehicles
table.

The navbar pill (`fleet_now_count`) is NOT the desk computation: four cheap
counts (overdue returns, open ground/soon issues, units with a lit code,
expired paperwork), cached a minute, shown only to fleet managers and founders.

## Planning downtime around demand

`fleet_capacity.py`. Two measures, deliberately, because each is wrong alone:

- **Booked peak** — the most legs in flight at one moment per vehicle tier,
  from `day_setup.peak_concurrency` (the founder's roster-sizing rule). Exact
  for what is booked; a floor three weeks out, because bookings keep arriving.
- **Typical use** — median distinct units the board actually ran on that
  weekday over the last 8 weeks (`DriverVehicleAssignment`). Blind to bookings;
  knows a Saturday needs sixteen cars before Saturday's bookings do.

A day is *clear* when both leave a spare car, *tight* when one lands exactly on
the fleet, *conflict* when one needs more cars than would be left. Tier
arithmetic is the scheduler's nested compatibility: for every tier t, legs
needing tier ≥ t must fit in units of tier ≥ t (a Sprinter runs an SUV job, not
the reverse). Reasons read the way the office talks: "Sprinters: 5 needed at
9:30 AM, 4 left", "SUV or bigger: …", "Any car: …" — and only for the tier that
actually adds demand, so "any car: 2" never restates "Sprinters: 2".

The window is judged twice — as the fleet stands, and with this unit gone —
and the difference is the answer: a Saturday that is short with every car is
dispatch's Saturday, not this decision, so the summary says *"already short with
every car — this car doesn't change that"* rather than crying wolf. The verdict
**informs, never blocks**: the downtime form runs it live as the dates are
typed, a day that **this car** tips into short comes back `409 needs_ack`, and a
tick saves it anyway — the system informs, a person decides. The verdict is stored on the row
so the report can say how often downtime landed on a clear day *given what was
known at the time*. The Outlook and the desk's window finder work by the HOUR
(`fleet_windows.py`): for every day, every shop square (7 AM – 5 PM) and every
tier, the most legs in flight at once that need that tier or bigger — the same
event sweep and the same `estimate_job_end_time` as `peak_concurrency`, so a
square can never disagree with the planner. Both screens read ONE payload
(`window_payload`) and one JS model (`includes/_fleet_windows_js.html`, inlined like every other dispatching page script)
ranks the windows: cost = squares where spare ≤ 0, then the tightest square,
then soonest; days this unit *tips* into short or tight rank last. The badge
beside an Outlook day is `judge_day` with one unit of that tier removed — the
downtime form's own check — and reads Short/Tight only when the unit worsens
the day (`if_down.worsened`); otherwise the squares' spare sign decides, so a
Saturday that is short with every car does not cry wolf at a Sprinter. "Book
it in" creates a normal day-level `VehicleDowntime` (the board plans by day)
with the hours in `reason`, through `fleet_save_downtime` and its `409
needs_ack` flow.

Demand for a window is cached 15 minutes (`OUTLOOK_CACHE_SECONDS`); supply (the
ledger) never is, so the page that edits it sees its own edit.

## Fault episodes

`samsara_service.extract_fault_codes` pulls the individual codes out of the
`faultCodes` block (same ECU-nesting rules as `_count_faults`; pending codes
skipped; a lit lamp with no readable code is `MIL`), and
`fleet_sync.upsert_fault_episodes` keeps one open `VehicleFault` row per code:
new code → open, still lit → refresh, gone → resolve. Wired into the poller's
extended-stats pass; **never resolves on an absent answer** (a 500 from Samsara
is not "all clear"). A code that opens three episodes in 30 days is called out
as *recurring*. A fault is a line on the desk, not a text — transient codes
would train everyone to ignore the number.

## Alerts

`fleet_notify.py`. Two texts only: a reported issue the moment it's filed, and
a morning digest (window 6–9 AM local, DB-stamped like the nightly, only on days
the NOW group is non-empty, top four lines). Recipients: profiles flagged
`is_fleet_manager` + `FLEET_NOTIFY_PHONES`. Master switch `FLEET_ALERTS_ENABLED`
(default off); inert under `TESTING`. `should_send_digest()` returns before any
query when alerts are off, so the poller pays nothing for it.

## Report

`/dispatching/fleet/report/` — `fleet_report.build_report(start, end, today)`,
30 / 90 / 365 days. Availability = 1 − down unit-days / active unit-days;
downtime by category; most-down, most-maintenance (cost), least-reliable
(weighted trouble score); PM adherence (each preventative record vs the previous
one of its kind and the car's interval — the first of a kind is *unknown*, not
guessed); downtime placement (saved verdicts + whether it started on a
slower-than-median day); mileage balance within a type (flag at 1.5×); what
keeps repeating (issue titles and fault codes seen twice). Every figure says
its coverage; an em-dash is no reading, never zero.

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
5. **Issues from the driver app.** Today a chauffeur's "it's making a noise"
   is relayed by a dispatcher (source = *Chauffeur (relayed)*). A report button
   in the driver portal would cut the middle step — but that is a driver-app
   change and needs its own conversation (see the no-driver-automation rule).
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
    dispatching.tests_fleet dispatching.tests_samsara dispatching.tests_fleet_desk \
    dispatching.tests_vehicle_status dispatching.tests_vehicle_status_wiring \
    dispatching.tests_fleet_bulk dispatching.tests_fleet_windows \
    dispatching.tests_fleet_day dispatching.tests_fleet_inspection
```

`tests_fleet_desk` covers the ledger endpoints, the demand check (pure and
against real legs), the attention rules, fault episodes, alerts, the report and
the fleet-manager experience (landing, top bar, login redirect).

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
