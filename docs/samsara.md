Ready for review
Select text to add comments on the plan
Plan — Samsara Integration Audit Document
Context
The user wants an audit-only deliverable: a markdown document at docs/samsara_integration_audit.md that maps how Samsara fleet telematics could enhance the existing Grayson Towncar Django dispatch backend.

Hard constraints from the prompt (no production code, no migrations, no model changes, no new dispatch behavior, no Samsara routes/tracking yet): this turn produces one markdown file — nothing else. The doc must be codebase-grounded, not generic API advice.

The driving question:

"How can Samsara help us know a trip may go wrong before the guest or dispatcher finds out too late?"

I explored the codebase with three Explore agents (reservation/trip data layer, dispatch workflow/UI, integrations/infrastructure) and a follow-up grep to confirm FleetVehicle exists. The plan below captures everything I need to write the audit doc — once approved via ExitPlanMode, I will transcribe Section "Final Audit Document" verbatim into docs/samsara_integration_audit.md.

Critical Codebase Facts (used to ground every recommendation)
These shape the entire architecture and must not be re-discovered later.

FleetVehicle exists at drivers/models.py:469 — vehicle*number (unique), vehicle_type (FK → rates.Vehicle), year, make, model, notes. This is the per-physical-car identity hook for Samsara. We do not need a new model for vehicle identity.
DriverLocation already exists at reservations/models.py:2615 — stores GPS snapshots (lat/lng/heading/speed/eta_minutes/eta_destination) from the driver app. Samsara complements this, doesn't replace it (driver app gives status-triggered snapshots; Samsara gives always-on telematics).
Live-ETA pattern already exists — \_annotate_legs_with_live_eta computes ETA via Google Maps Distance Matrix when a driver hits "on-the-way." Samsara plugs into this same surface, no new UI needed for Phase 1 ETA values.
AeroAPI service is the closest analog — dispatching/aeroapi_service.py:25 uses requests.Session() + auth header, returns {status, ...} dicts, catches rate-limit (429) and 404 specifically. Mirror this for Samsara.
No Celery workers, single gunicorn worker on Railway (memory + railway.json). Background work uses \_run_in_background() daemon thread at reservations/utils.py:22. For recurring polling, the project uses ghl_integration/scheduler.py — an in-process daemon thread guarded by a Postgres advisory lock (pg_try_advisory_lock(737201)) so it runs in exactly one worker. This is the pattern to follow for Samsara polling.
No map UI anywhere in dispatch — dashboard timelines are text-based (dispatching/templates/dispatching/legs_filter.html). Customer-side has Google Maps autocomplete only. So adding a map in dispatch is a non-trivial UI addition; Phase 1 must avoid it — use text-only fields (location label + last-seen-age + ETA) first.
No websockets / no push — every dashboard is pull-on-pageload. Live data flows in via prefetch on render. Samsara data must follow the same model: cache the latest snapshot, render at request time, optionally poll-refresh via a tiny JS fetch later.
Reservation detail page (dispatching/views.py:1429 → reservation_view.html) is the highest-value injection point.
Pickup time is naive — pickup_date (DateField) + pickup_time (TimeField). Combine via timezone.datetime.combine() + make_aware. All risk-engine pseudocode must respect this.
Pickup/dropoff are free text on Leg (not FK to Location). For geofence destination matching, we'll need lookup by keywords (the AIRPORT_KEYWORDS/CRUISE_PORT_KEYWORDS lists in reservations/models.py already do this for trip_type classification — reuse).
Status taxonomy on Leg: in-progress, confirmed, on-the-way, on-location, picked-up, completed, cancelled. Samsara-driven alerts must be silent on completed/cancelled.
Env-var convention: python-dotenv + os.environ.get(), screaming-snake names (STRIPE_SECRET_KEY, AEROAPI_KEY, TWILIO_AUTH_TOKEN). Use SAMSARA_API_TOKEN for the bearer token. Webhook secret as SAMSARA_WEBHOOK_SECRET (matches STRIPE_WEBHOOK_SECRET).
Logging: logger = logging.getLogger(**name**) everywhere; the only named logger is perf for slow requests. No Sentry. Treat integration errors as "log + return error dict, don't raise."
Caching: Redis if REDIS_URL set, else LocMemCache. Existing pattern used by capacity_planner (60s TTL, key capacity_planner*{date}). Use samsara_vehicle_snapshot:{vehicle_id} for live position with short TTL.
Existing management command pattern — dispatching/management/commands/dispatch_alerts.py already exists. Samsara-driven trip-risk alerts can extend this rather than create a new alert framework.
Samsara coverage is partial and growing — by design. Per the account owner: not all in-house vehicles are in Samsara today; vehicles are being onboarded incrementally. Affiliate drivers will never be in Samsara. This means the entire integration must treat "no Samsara mapping for this leg's vehicle" as the normal case, not an error state — render nothing (don't pollute the UI with grey badges for un-onboarded fleet) and short-circuit risk evaluation silently.
What I'll Build (the actual deliverable)
One file: docs/samsara_integration_audit.md, ~1500–2000 lines of markdown, structured exactly per the section list in the prompt:

Executive Summary
Current Backend Findings
Current Dispatch Workflow
Current Operational Gaps
Where Samsara Fits
Recommended Samsara Use Cases
Data Model Recommendations
Service Layer Design
Background Job Strategy
Webhook Strategy
UI/Dashboard Recommendations
ETA and Late-Risk Logic (pseudocode)
Phased Implementation Roadmap (Phases 0–6)
Open Questions
Things Not To Implement Yet
Section content is drafted below. After approval I'll transcribe; no other files change. No commits, no migrations, no settings changes.

Headline Recommendations (drive the audit doc)
Top 5 Samsara opportunities (ranked by value)
Pre-pickup late-risk detection — combine Samsara live position + distance-to-pickup vs scheduled_pickup - now to flag at-risk trips before the customer notices. This is the direct answer to the driving question. Surfaces on dispatch dashboard (legs_filter.html) and reservation detail page as a colored badge with reason text.
"Vehicle hasn't moved" alert for upcoming MCO/Port pickups — Samsara movement_status + idle_duration. Catches the "vehicle still at the warehouse 30 min before MCO" scenario the prompt explicitly calls out. Cheap to implement once Phase 1 polling exists.
Live-ETA enhancement on dispatch dashboard — today the dashboard shows GPS-based ETA only when the driver hits "on-the-way" (drivers/views.py:103). With Samsara we can show ETA for any upcoming trip whose vehicle is on the road, not just in-progress ones. Dispatchers stop relying on driver compliance.
Always-on vehicle visibility (last-seen, status, location label) — adds a "Where is the vehicle right now?" answer to the reservation detail page and the dispatch dashboard's in-house driver cards. Zero downstream complexity; foundation for everything else.
Geofence arrival events for MCO / Port Canaveral / warehouse — turn "driver arrived at pickup" from manual status-tap into automatic event. Reduces missed status updates that silently break SLA timers.
Recommended Phase 1 scope (minimum viable, audit-aligned)
Account/token setup + manual mapping FleetVehicle → samsara_vehicle_id.
One service class: dispatching/samsara_service.py (mirror AeroAPI shape).
One model change (Phase 1 only): add 5 fields to FleetVehicle (samsara_vehicle_id, samsara_last_latitude, samsara_last_longitude, samsara_last_seen_at, samsara_last_synced_at). No new tables.
One background poller: extend ghl_integration/scheduler.py pattern (in-process daemon + PG advisory lock) to fetch /fleet/vehicles/stats?types=gps every 2–5 min.
Read-only UI additions on two pages: dispatch dashboard in-house driver card ("last seen 2m ago — near MCO") and reservation detail page ("Vehicle: V3 — last position: …"). Text only, no map.
No risk engine, no webhooks, no customer tracking, no DVIR.
Major unknowns / blockers
(Owner is the Samsara account holder and can verify all API/plan details directly, so these are quick lookups, not project blockers.)

Samsara plan-tier entitlements — confirm Vehicle Stats, Geofences, Webhooks, and Customer Live Sharing are all available on the current plan before Phases 4 and 5. Owner can verify.
Webhook signing scheme — exact header name and HMAC algorithm. Owner can grab from Samsara docs when Phase 4 starts.
FleetVehicle ↔ Samsara mapping flow — since vehicles are being onboarded incrementally, the Phase 1 management command should list un-mapped FleetVehicles and available Samsara vehicles side-by-side to make the periodic onboarding ritual cheap.
Authoritative pickup-time for risk calc — when a Leg has a flight, the controlling-flight's best-available arrival (reservations/models.py:1908+) should override leg.pickup_time. Risk engine must use the effective pickup time, not the stored one. The doc states this explicitly.
Drive-time source for "vehicle is X minutes from pickup" — Samsara's native ETA may or may not exist on plan; alternative is Google Maps Distance Matrix (already used in \_annotate_legs_with_live_eta). The doc recommends Google Maps for Phase 2 since it's already wired; revisit if Samsara native ETA is cheaper/better.
Phase 1 polling cost — small fleet polled every 2–5 min is well within Samsara rate limits, but confirm once token is live.
Final Audit Document — Draft Content
The following will be written verbatim to docs/samsara_integration_audit.md after ExitPlanMode approval. (Sections shortened here where the prompt template already dictates the headings; full prose below.)

# Samsara Integration Audit

> **Scope:** Audit + architecture planning only. No code, no migrations, no
> model changes, no Samsara routes, no customer tracking yet.
> **Goal:** Answer "How can Samsara help us know a trip may go wrong before
> the guest or dispatcher finds out too late?" — grounded in this codebase.

## 1. Executive Summary

Grayson Towncar's dispatch backend already knows **what should happen**
(scheduled pickup, assigned driver, assigned vehicle) but cannot see **what
is actually happening** to the vehicle in the real world. Samsara closes
that gap. The highest-value uses are, in order:

1. **Pre-pickup late-risk detection** — compare Samsara live position with
   scheduled pickup time and flag trips at risk _before_ the customer
   notices. Directly answers the audit's driving question.
2. **"Vehicle hasn't moved" alerts** for MCO and Port Canaveral pickups,
   using Samsara movement/idle status near pickup time.
3. **Always-on dispatch-dashboard visibility** — last-seen, location label,
   current movement status for every in-house vehicle, every page load.
4. **Live ETA for upcoming trips** — extends today's
   [`_annotate_legs_with_live_eta`](drivers/views.py#L103) so ETA is
   available regardless of whether the driver has hit "on-the-way."
5. **Geofence arrival/departure** events at MCO, the warehouse, and Port
   Canaveral to auto-trigger status transitions and reduce manual taps.

Recommended posture: **Grayson backend stays the brain** (reservations,
dispatch, assignment, payments, schedule, customer comms). **Samsara is a
visibility/early-warning layer.** Do not let Samsara own assignment, status
truth, or customer-facing tracking until later phases — and only if the
basics prove out.

## 2. Current Backend Findings

### Core models

| Model                                | Location                                                          | Purpose                                                                                                                                                                                                                                                                                                   |
| ------------------------------------ | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Customer`                           | [reservations/models.py:19](reservations/models.py#L19)           | Identity (email/phone), Stripe customer id, card metadata.                                                                                                                                                                                                                                                |
| `Reservation`                        | [reservations/models.py:61](reservations/models.py#L61)           | Header for a trip: customer, vehicle (class), pricing, paid state (`is_paid`, `paid_amount`, `first_paid_at`), status, refund state, UTM attribution. Many `@cached_property`-derived totals.                                                                                                             |
| `Leg`                                | [reservations/models.py:841](reservations/models.py#L841)         | A single ride. Holds `pickup_date` (Date) + `pickup_time` (Time) (**naive**, combine with `timezone.datetime.combine` + `make_aware`), `pickup_location`/`dropoff_location` (**free text**, not FK), `status`, FK to `Driver`/`Route`/`Vehicle`/controlling `Flight`/`Cruise`, plus driver-pay breakdown. |
| `LegStatus`                          | [reservations/models.py:2556](reservations/models.py#L2556)       | Append-only status history for a Leg with timestamp + updated_by.                                                                                                                                                                                                                                         |
| `Flight`                             | [reservations/models.py:1908](reservations/models.py#L1908)       | AeroAPI-populated. Has scheduled/estimated/actual gate-and-runway arrival times. Use the "best-available" precedence when computing effective pickup time.                                                                                                                                                |
| `LegFlight`                          | [reservations/models.py:1855](reservations/models.py#L1855)       | Through-model for multi-flight legs; `is_controlling=True` marks the flight that drives pickup timing.                                                                                                                                                                                                    |
| `LegStop`                            | [reservations/models.py:1756](reservations/models.py#L1756)       | Intermediate stops within a Leg.                                                                                                                                                                                                                                                                          |
| `Payment`                            | [payment/models.py:10](payment/models.py#L10)                     | Stripe payment record; signal at [payment/signals.py:52](payment/signals.py#L52) keeps `Reservation` paid fields in sync.                                                                                                                                                                                 |
| `Driver`                             | [drivers/models.py:8](drivers/models.py#L8)                       | Profile, schedule defaults, type (inhouse/affiliate), pay rules. `is_active` toggle.                                                                                                                                                                                                                      |
| `DriverWeeklySchedule`               | [drivers/models.py:226](drivers/models.py#L226)                   | Per-day-of-week availability.                                                                                                                                                                                                                                                                             |
| `DriverDateOverride`                 | [drivers/models.py:304](drivers/models.py#L304)                   | One-off time-off / availability change. Recent feature: driver self-serve with founder approval.                                                                                                                                                                                                          |
| **`FleetVehicle`**                   | [drivers/models.py:469](drivers/models.py#L469)                   | **Per-physical-car identity** — `vehicle_number` (unique), `year`/`make`/`model`, `vehicle_type` FK → `rates.Vehicle`. **This is the Samsara mapping point.**                                                                                                                                             |
| `DriverVehicleAssignment`            | [drivers/models.py:487](drivers/models.py#L487)                   | Links a Driver to a FleetVehicle for a single date (in-house only).                                                                                                                                                                                                                                       |
| `Vehicle` (`rates.Vehicle`)          | [rates/models.py:5](rates/models.py#L5)                           | Vehicle **class** (towncar, SUV, mini-van, van, 14-pax van) used for pricing/capacity — **not** an individual car.                                                                                                                                                                                        |
| `Route`, `Location`, `LocationGroup` | [rates/models.py:96-168](rates/models.py#L96)                     | Origin/destination naming and rates. Pickup/dropoff text is matched against Location names/aliases for route inference.                                                                                                                                                                                   |
| `Rate`                               | [rates/models.py:168](rates/models.py#L168)                       | Vehicle × Route pricing.                                                                                                                                                                                                                                                                                  |
| `DriverLocation`                     | [reservations/models.py:2615](reservations/models.py#L2615)       | **Existing GPS snapshots**: lat/lng/accuracy/heading/speed, plus `eta_minutes` and `eta_destination`. Written when the driver app reports status changes.                                                                                                                                                 |
| `AuditLog`                           | [reservations/models.py:2451](reservations/models.py#L2451)       | Generic audit trail for model changes.                                                                                                                                                                                                                                                                    |
| `Lead`, `Quote`                      | [reservations/models.py:2120, 2292](reservations/models.py#L2120) | Pre-booking funnel, GHL-synced.                                                                                                                                                                                                                                                                           |

### Integrations

- **Stripe** — client `stripe.api_key = settings.STRIPE_SECRET_KEY`. Webhook at [payment/webhook.py:24](payment/webhook.py#L24) (`@csrf_exempt`, signature via `stripe.Webhook.construct_event`, idempotency by `stripe_checkout_id`, returns 200 even on internal failure so Stripe doesn't retry).
- **Twilio SMS** — [dispatching/confirmation_sms.py:389](dispatching/confirmation_sms.py#L389), client built per-call from settings. Batch send via `send_confirmations_for_date()` at line 424.
- **AeroAPI (FlightAware)** — [dispatching/aeroapi_service.py:25](dispatching/aeroapi_service.py#L25). `requests.Session()` with `x-apikey` header set once, smart endpoint selection (`/flights/` if within 48 h, else `/schedules/`), returns dicts with `status: success|not_found|rate_limited|error`. **This is the analog to copy for Samsara.**
- **GoHighLevel** — dedicated `ghl_integration/` app with its own scheduler.
- **Google Maps Distance Matrix** — used in [`_annotate_legs_with_live_eta`](drivers/views.py#L103) to compute ETA from a `DriverLocation` to a destination address.
- **Email** — `EmailMultiAlternatives`, SMTP creds from env, sent via `_send_email_with_retry()` exponential backoff (1s/2s/4s) in a daemon thread. Trigger for booking confirmation: [payment/webhook.py:223](payment/webhook.py#L223).

### Infrastructure

- **Async pattern**: [`_run_in_background(func, *a, **kw)`](reservations/utils.py#L22) — daemon thread, try/except logs but doesn't propagate. Used widely for email, SMS-batch, Meta events.
- **Recurring jobs**: **no Celery worker in prod.** Instead, [ghl_integration/scheduler.py](ghl_integration/scheduler.py) runs **inside gunicorn** as a daemon thread, polls every 30 min, and uses **`pg_try_advisory_lock(737201)`** so only one worker executes (memory says single worker today, but the lock is the right safeguard). Started from [ghl_integration/apps.py](ghl_integration/apps.py) on Django ready.
- **Deployment**: Railway, single replica, `gunicorn business.wsgi`, no separate worker dyno. See [railway.json](railway.json).
- **Env vars**: `python-dotenv` + `os.environ.get()`. Convention: screaming-snake, prefixed by service (`STRIPE_*`, `TWILIO_*`, `AEROAPI_*`, `GHL_*`, `GOOGLE_MAPS_*`).
- **Logging**: `logger = logging.getLogger(__name__)`; only named logger is `perf` (slow-request middleware in [business/middleware.py](business/middleware.py), threshold 500 ms). No Sentry.
- **Caching**: `RedisCache` if `REDIS_URL` set, else `LocMemCache`. Existing usage: `capacity_planner_{date}` 60 s TTL.
- **Management commands**: [dispatching/management/commands/dispatch_alerts.py](dispatching/management/commands/dispatch_alerts.py), [reservations/.../update_completed_reservations.py](reservations/management/commands/update_completed_reservations.py), [ops/.../send_unpaid_reminders.py](ops/management/commands/send_unpaid_reminders.py), and ~13 more.

## 3. Current Dispatch Workflow

**Reservation creation** → `reservation_form()` view in
[reservations/views.py](reservations/views.py); creates a `Reservation`
plus 1–N `Leg`s. Signals fire confirmation email and lead-conversion sync.

**Driver assignment** → AJAX POST to
[`/dispatch/update-leg-assignment/`](dispatching/views.py#L1940). Sets
`leg.driver`, `driver_assigned_by`, `driver_assigned_at`. Invalidates
`capacity_planner_{date}` cache.

**Vehicle assignment** (in-house) → AJAX POST to
[`/dispatch/update-inhouse-vehicle-assignment/`](dispatching/views.py#L2283).
Creates/updates `DriverVehicleAssignment(driver, date, vehicle)`. Vehicles
are assigned per-date, not per-leg.

**Reservation detail** → [dispatching/views.py:1429](dispatching/views.py#L1429)
→ [reservation_view.html](dispatching/templates/dispatching/reservation_view.html).
Shows passenger, payment, legs (with inline driver/vehicle/status dropdowns),
flight/cruise info, audit history, refund state.

**Dispatch dashboard** → [dispatching/views.py:93](dispatching/views.py#L93)
(`index`) → [legs_filter.html](dispatching/templates/dispatching/legs_filter.html).
Date-filtered legs table, in-house driver cards (mini timeline + vehicle +
shift), unassigned slots with smart-fill suggestions, coverage stats.
**No map. No auto-refresh. No websocket.**

**Upcoming trips** → [`legs_list`](dispatching/views.py#L1746) → paginated
table, filters for date/status/driver/vehicle/trip_type.

**Capacity planner** → [`capacity_planner`](dispatching/views.py#L8010) →
horizontal timeline per in-house driver, gap-fill suggestions. 60 s cache
keyed on date, invalidated on assignment change.

**Driver weekly schedule** → [`drivers/views.py:256`](drivers/views.py#L256)
→ [weekly_schedule.html](drivers/templates/drivers/weekly_schedule.html).
Shows next 60 days of legs to the driver. Includes flight delay alerts and
live ETA when en route.

**Time-off** — driver self-serve at
[`request_timeoff`](drivers/views.py#L1498), founder approval queue at
[`dispatcher_timeoff_requests`](dispatching/views.py#L14876). Backed by
`DriverDateOverride` with `status` field.

**Status taxonomy**

| Model                               | Statuses                                                                                                 |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Reservation                         | `pending`, `confirmed`, `completed`, `cancelled`                                                         |
| Leg                                 | `in-progress` (default), `confirmed`, `on-the-way`, `on-location`, `picked-up`, `completed`, `cancelled` |
| Payment                             | `pending`, `card_saved`, `paid`, `failed`, `refunded`                                                    |
| Leg.payment_status (driver payroll) | `unpaid`, `paid`, `canceled`                                                                             |
| DriverDateOverride                  | `pending`, `approved`, `denied`, `cancelled`                                                             |

**Status transitions** are dispatcher-driven (dropdown in
`reservation_view.html`) or driver-driven (via
[`/drivers/update_leg_status/<leg_id>/`](drivers/views.py)). Every change
writes a `LegStatus` row. When a driver flips to `on-the-way`, a background
thread computes ETA via Google Maps and writes a `DriverLocation` snapshot.

## 4. Current Operational Gaps

Each gap below is verified against the actual codebase, not speculative.

| #   | Gap                                                                                                                                                                                      | Evidence                                                                                                                   |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| G1  | **No always-on vehicle location.** GPS exists only when the driver app reports a status change. If the driver app crashes, backgrounded, or driver forgets to update, dispatch is blind. | `DriverLocation` is written only inside [drivers/views.py:375-380](drivers/views.py#L375) (status-change hook).            |
| G2  | **No pre-pickup risk signal.** Dispatch knows pickup time and the assigned vehicle, but nothing compares them in real time. The dashboard has no "this trip is at risk" badge.           | `legs_filter.html` shows turnaround warnings only, not pickup-feasibility.                                                 |
| G3  | **"Vehicle still at the warehouse"** — backend has no movement_status concept.                                                                                                           | No such field on `Driver`, `Leg`, or `FleetVehicle`.                                                                       |
| G4  | **ETA only for in-progress trips.** A driver who hasn't tapped "on-the-way" produces no ETA, even if they're 50 minutes from MCO.                                                        | [`_annotate_legs_with_live_eta`](drivers/views.py#L103) filters to `status in ["on-the-way", "picked-up", "on-location"]`. |
| G5  | **No "vehicle arrived" detection.** Status flips to `on-location` only by driver tap, missing arrivals cause silent SLA misses.                                                          | No geofence model exists.                                                                                                  |
| G6  | **Flight-driven re-timing isn't paired with vehicle position.** AeroAPI tells us MCO landing slipped 25 min; nothing checks whether the vehicle's ETA can absorb the slip.               | AeroAPI updates `Flight.actual_*` fields but no downstream re-evaluation of pickup risk.                                   |
| G7  | **No customer tracking link.** Guests email/call dispatch for "where is my driver?"                                                                                                      | No `live_tracking_url` field anywhere on `Reservation`/`Leg`/`Customer`.                                                   |
| G8  | **Vehicle readiness invisible.** Dispatch can't tell if FleetVehicle V3 is offline, low fuel, or has an open DVIR before assigning it.                                                   | `FleetVehicle` has no health/state fields.                                                                                 |
| G9  | **No alert when vehicle leaves geofence late.** "Vehicle hasn't left the warehouse 30 min before an MCO pickup" requires both location and movement signals.                             | Not derivable from any current data.                                                                                       |

Affiliate drivers are out of scope for Samsara coverage — they're not in
the Samsara fleet — and any risk engine must short-circuit cleanly for
`driver.driver_type == "affiliate"`.

## 5. Where Samsara Fits

┌──────────────────────────────────────────────────────────────────┐ │ GRAYSON BACKEND (truth) │ │ Reservations · Legs · Drivers · FleetVehicle · Assignments │ │ Payments · Schedules · Customer comms · Dispatch UI · AeroAPI │ └────────────────────────────┬─────────────────────────────────────┘ │ reads from ▼ ┌──────────────────────────────────────────────────────────────────┐ │ SAMSARA (visibility layer) │ │ Vehicle GPS · Movement/idle · ETA · Geofence events · DVIR/fuel │ │ Customer Live Sharing links (later phase) │ └──────────────────────────────────────────────────────────────────┘

**Rules:**

- Samsara never assigns a driver, never creates/edits a reservation, never
  charges a card, never sends a customer message on its own.
- Samsara writes only to **new** fields/tables. It does **not** mutate
  `Leg.status`, `LegStatus`, or `DriverLocation`.
- The backend is the system of record; Samsara is a sensor. If Samsara is
  down, dispatch keeps working — just without the early-warning badges.
- **Partial coverage is the steady state.** Not every in-house vehicle is
  in Samsara today (the fleet is being onboarded gradually), and
  **affiliate drivers are permanently out of scope** for Samsara
  telematics. The integration treats legs whose effective vehicle has no
  `samsara_vehicle_id` exactly like today — no badge, no banner, no
  "unknown" warning — so the dispatcher's existing manual workflow is
  unaffected. Samsara only ever _adds_ visibility; it never subtracts.

## 6. Recommended Samsara Use Cases

### A. Live vehicle visibility (Phase 1)

- **Where it appears:** dispatch dashboard in-house driver cards
  (`legs_filter.html`) and reservation detail page header
  (`reservation_view.html`).
- **Data shown:** last known position label (reverse-geocoded once and
  cached, e.g. "near MCO"), last-seen age ("2 min ago"), movement status
  (driving / idle / off).
- **Storage strategy:** store latest snapshot on `FleetVehicle` itself
  (last-write-wins fields). Avoid a per-snapshot history table in Phase 1
  — Samsara is the system of record for telemetry; we only need "latest."
- **Sync cadence:** poll every 2–5 minutes via the in-process scheduler
  (Sec. 9). No webhooks in Phase 1.

### B. ETA and late-risk detection (Phase 2)

See Sec. 12 for full pseudocode. Stores `dispatch_risk_status` +
`dispatch_risk_reason` on `Leg`. Surfaces as a badge on dispatch dashboard
and as a banner on the reservation detail page.

### C. Route progress (Phase 3, optional)

Only if Phase 2 proves stable and dispatchers ask for it. Would add
`samsara_route_id` to `Leg` and create a Samsara route from the leg's
pickup/dropoff when the driver is assigned. Could auto-flip `Leg.status`
to `on-the-way`/`on-location`/`picked-up`/`completed` based on route
events. **Risk:** competes with driver-app status updates; risks double
truth. Recommendation: keep manual status authoritative; Samsara only
suggests via a "Samsara says picked-up at 14:32 — accept?" prompt.

### D. Geofences and location events (Phase 4)

Recommended geofences:

| Geofence                       | Purpose                               |
| ------------------------------ | ------------------------------------- |
| MCO terminals (curbside zones) | "Vehicle arrived at pickup curb"      |
| Port Canaveral terminals       | Same, plus cruise window pressure     |
| Warehouse / staging            | "Vehicle hasn't left warehouse" alert |
| Disney resort cluster          | Coarse zone for pickup proximity      |
| Universal resort cluster       | Same                                  |

Drives webhook events (Sec. 10). Phase 4, not Phase 1.

### E. Customer tracking (Phase 5)

Generate a Samsara Customer Live Sharing link per leg when driver hits
`on-the-way`. Store on `Leg.live_tracking_url`. **Send manually first**
(dispatcher button: "Send tracking link to guest"). Auto-send only after
several weeks of manual use proves it works for both directions
(MCO→hotel and hotel→MCO). Requires verification that Samsara plan
includes Live Sharing.

### F. Vehicle readiness (Phase 6)

Pre-shift check: when assigning a `FleetVehicle` to a driver for a date,
surface Samsara DVIR open defects, last-seen age (offline > 12 h →
warning), and fuel level if available. Recommendation: render warnings
in the existing in-house vehicle assignment UI
([dispatching/views.py:2283](dispatching/views.py#L2283)); never block
assignment automatically.

## 7. Data Model Recommendations

**Minimal additions only.** Do not add fields we don't yet have a place to
display. All fields are nullable so legacy rows and affiliate-driver legs
work unchanged.

### Phase 1 — on `FleetVehicle` ([drivers/models.py:469](drivers/models.py#L469))

| Field                         | Type                                                                  | Why                                                                                                   |
| ----------------------------- | --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `samsara_vehicle_id`          | `CharField(max_length=64, blank=True, db_index=True)`                 | The mapping point. Unique-per-vehicle.                                                                |
| `samsara_last_latitude`       | `DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)` | Matches `DriverLocation` precision.                                                                   |
| `samsara_last_longitude`      | `DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)` | Same.                                                                                                 |
| `samsara_last_location_label` | `CharField(max_length=128, blank=True)`                               | Cached reverse-geocode string for UI ("near MCO Terminal A"). Reduces every-page-load geocoding cost. |
| `samsara_last_seen_at`        | `DateTimeField(null=True, blank=True, db_index=True)`                 | When the GPS sample was taken (Samsara's timestamp, not ours).                                        |
| `samsara_movement_status`     | `CharField(max_length=32, blank=True)`                                | `driving`, `idle`, `off` (string mirrors Samsara field).                                              |
| `samsara_last_synced_at`      | `DateTimeField(null=True, blank=True)`                                | When _we_ last successfully polled. Diagnostic.                                                       |

### Phase 2 — on `Leg` ([reservations/models.py:841](reservations/models.py#L841))

| Field                        | Type                                                                                                           | Why                                        |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `dispatch_risk_status`       | `CharField(max_length=16, blank=True, db_index=True)` choices: `normal`, `watch`, `at_risk`, `late`, `unknown` | Drives the badge color.                    |
| `dispatch_risk_reason`       | `CharField(max_length=255, blank=True)`                                                                        | Human-readable reason text shown on hover. |
| `dispatch_risk_evaluated_at` | `DateTimeField(null=True, blank=True)`                                                                         | Staleness check.                           |
| `live_eta_pickup`            | `DateTimeField(null=True, blank=True)`                                                                         | Vehicle's projected arrival at pickup.     |

### Phase 3 — on `Leg` (only if routes adopted)

| Field              | Type                                                  | Why                    |
| ------------------ | ----------------------------------------------------- | ---------------------- |
| `samsara_route_id` | `CharField(max_length=64, blank=True, db_index=True)` | Link to Samsara route. |

### Phase 5 — on `Leg`

| Field                   | Type                                   | Why                  |
| ----------------------- | -------------------------------------- | -------------------- |
| `live_tracking_url`     | `URLField(max_length=500, blank=True)` | Customer share link. |
| `live_tracking_sent_at` | `DateTimeField(null=True, blank=True)` | Audit + dedupe.      |

### Phase 6 — on `FleetVehicle`

| Field                     | Type                                  | Why                |
| ------------------------- | ------------------------------------- | ------------------ |
| `samsara_fuel_percent`    | `IntegerField(null=True, blank=True)` | Pre-shift display. |
| `samsara_open_dvir_count` | `IntegerField(null=True, blank=True)` | Pre-shift warning. |

**Fields explicitly NOT recommended:** none on `Driver` (Samsara driver-id
mapping is unnecessary if we route everything through `FleetVehicle` →
`DriverVehicleAssignment`). No new `SamsaraVehicleSnapshot` history table
in Phase 1 (Samsara already keeps that). No `live_eta_dropoff` until a
dispatcher actually asks for it.

## 8. Service Layer Design

Mirror the AeroAPI shape — co-located with its consumer, single class,
session reuse, returns dicts.
dispatching/ ├── aeroapi_service.py # existing ├── samsara_service.py # NEW — client + service methods └── samsara_risk.py # NEW (Phase 2) — pure risk-logic functions

`dispatching/samsara_service.py` (sketch — not implementation):

```python
class SamsaraService:
    def __init__(self):
        self.api_token = settings.SAMSARA_API_TOKEN
        self.base_url = "https://api.samsara.com"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        })

    def get_vehicle_stats(self, vehicle_ids=None, types=("gps",)):
        """Returns {'status': 'success'|'rate_limited'|'error', 'data': [...], ...}"""

    def get_vehicle_location(self, samsara_vehicle_id):
        ...

    def list_vehicles(self):
        """Used once by an admin command to populate the mapping."""
Errors follow AeroAPI conventions: catch 429 → return {"status": "rate_limited", "retry_after": ...}; catch 404 → {"status": "not_found"}; catch network → log + return {"status": "error", "error": str(e)}. Never raise to the caller.

Phase 2 splits risk logic into samsara_risk.py so the rules are unit-testable without HTTP mocking.

Phase 4 adds dispatching/samsara_webhooks.py for inbound events. Phase 5 adds samsara_tracking.py for customer link generation. Phase 6 adds samsara_dvir.py for readiness checks.

9. Background Job Strategy
Use the existing in-process daemon scheduler pattern. Do not introduce Celery. Do not introduce a separate Railway worker process.

Extend ghl_integration/scheduler.py (or create a sibling dispatching/samsara_scheduler.py modeled on it) with a 2–5 min interval task.
Guard with pg_try_advisory_lock(<unique constant>) so it never double-runs even if Railway scales to 2 workers later.
Single-task body:
fleet = FleetVehicle.objects.exclude(samsara_vehicle_id='')
Call samsara_service.get_vehicle_stats(vehicle_ids=[...], types=('gps',))
bulk_update samsara_last_* fields on FleetVehicle.
Log counts to the existing logger.
Failure mode: log + continue. Never crash the scheduler loop (mirror the GHL pattern).
For one-off operations (initial mapping import, manual sync, debugging), add a management command: dispatching/management/commands/samsara_sync_vehicles.py — mirrors dispatch_alerts.py.

Phase 2 (risk evaluation) does not need its own poller. The risk badge can be computed at request time from the cached FleetVehicle snapshot when the dispatch dashboard or reservation page is rendered, with results cached briefly (e.g., 60 s) keyed on leg_id. This keeps Phase 2 write-light. Only escalate to a scheduled "risk sweep" if dispatchers ask for proactive notifications (e.g., ntfy push when a leg flips to at_risk).

Phase 4 webhooks bypass polling entirely for arrival/departure events.

10. Webhook Strategy
Phase 1: no webhooks. Polling is sufficient for vehicle visibility.

Phase 4: add webhooks for geofence events only. Route at /integrations/samsara/webhook/. Mirror the Stripe pattern:

@csrf_exempt on the view.
Signature verification using SAMSARA_WEBHOOK_SECRET (needs verification against official Samsara docs for the exact header and HMAC algorithm).
Return 200 immediately; queue downstream work via _run_in_background() so Samsara doesn't retry on slow processing.
Idempotency: dedupe by Samsara's event id (cache key samsara_event:{id} with 24 h TTL).
Errors logged via standard logger.
Useful event types (Phase 4):

Geofence entry/exit (MCO, Port Canaveral, warehouse).
Vehicle disconnected (write a dispatch_risk_status='unknown' for any in-progress leg using that vehicle).
Optionally: DVIR submission (Phase 6).
Webhook events that should mutate the database: geofence entry/exit at pickup/dropoff zones (sets dispatch_risk_status, optionally suggests a status flip). Events that should only log: speeding alerts, harsh-driving events — visibility but no action.

11. UI / Dashboard Recommendations
Phase 1 (no map, text only)
Render contract (applies to both pages below):

samsara_vehicle_id empty (vehicle not yet onboarded, or affiliate) → render nothing about Samsara. The row looks exactly like today.
samsara_vehicle_id set + samsara_last_seen_at fresh (≤15 min) → render position label + age + movement status normally.
samsara_vehicle_id set + samsara_last_seen_at stale (>15 min) or null → render greyed "Live position stale (15m+)" — only here do we surface a warning, because the vehicle is supposed to be visible and isn't.
dispatching/templates/dispatching/legs_filter.html (dispatch dashboard)

In-house driver card: add a thin line under vehicle: "📍 near MCO • 2m ago" when fresh.
No risk badge yet.
dispatching/templates/dispatching/reservation_view.html (reservation detail)

Below the per-leg "Vehicle: V3" line, add a small line: "Last position: near International Drive (2m ago, driving)."
Stale/null handled per render contract above.
Phase 2 (risk visible)
Dashboard table row: prepend a colored dot per leg — green (normal), yellow (watch), orange (at_risk), red (late), grey (unknown).
Reservation detail: banner above the leg list when any leg is at_risk or late, with the dispatch_risk_reason text.
Capacity planner (daily_capacity_planner.html): tint the leg block on the timeline by risk level. Cheap visual.
Phase 3 — UI for route progress
Reservation detail: show "Samsara says picked-up at 14:32. Accept?" prompt next to manual status dropdown. Never auto-overwrite manual status.
Phase 5 — customer tracking
Reservation detail (driver assigned + leg confirmed): add a "Send tracking link" button. Disabled if live_tracking_url empty.
After send: show "Sent 13:01" badge.
Phase 6 — readiness
In-house vehicle assignment modal (update_inhouse_vehicle_assignment): show fuel %, last-seen age, open DVIR count next to each vehicle option. Never block; just warn.
12. ETA and Late-Risk Logic
Pure-function pseudocode for samsara_risk.evaluate_leg_risk(leg):

def evaluate_leg_risk(leg) -> (status, reason):
    # 1. Skip statuses where risk is meaningless.
    if leg.status in {"completed", "cancelled"}:
        return ("normal", "")

    # 2. Skip silently when Samsara cannot help with this leg.
    #    Affiliates are permanently out of Samsara; in-house vehicles are
    #    onboarded gradually. In both cases the existing manual workflow
    #    is unchanged — render no badge at all (NOT "unknown", which would
    #    pollute the dashboard).
    if leg.driver and leg.driver.driver_type == "affiliate":
        return (None, "")  # caller renders nothing
    fleet_vehicle = resolve_assigned_fleet_vehicle(leg)  # via DriverVehicleAssignment
    if fleet_vehicle is None or not fleet_vehicle.samsara_vehicle_id:
        return (None, "")  # caller renders nothing

    # 3. Resolve effective pickup time.
    #    If a controlling flight exists, use best-available arrival
    #    + a buffer (e.g., 30 min for baggage). Otherwise scheduled pickup.
    effective_pickup = compute_effective_pickup_datetime(leg)
    if effective_pickup is None:
        return (None, "")  # nothing useful to compare against

    now = timezone.now()
    minutes_to_pickup = (effective_pickup - now).total_seconds() / 60

    # 4. Staleness check.
    if (now - fleet_vehicle.samsara_last_seen_at) > timedelta(minutes=15):
        return ("unknown", "Vehicle telematics stale (>15 min)")

    # 5. Compute drive time from vehicle to pickup.
    #    Phase 2: use Google Maps Distance Matrix (already wired) for parity
    #    with _annotate_legs_with_live_eta. Cache 60 s by (lat,lng,address).
    drive_min = google_maps_drive_minutes(
        origin=(fleet_vehicle.samsara_last_latitude,
                fleet_vehicle.samsara_last_longitude),
        destination=leg.pickup_location,
    )
    if drive_min is None:
        return ("unknown", "Could not compute drive time")

    slack = minutes_to_pickup - drive_min   # positive = on time

    # 6. Movement override: if pickup is close and the vehicle isn't moving,
    #    upgrade severity regardless of pure slack.
    is_idle_or_off = fleet_vehicle.samsara_movement_status in ("idle", "off")
    if minutes_to_pickup <= 45 and is_idle_or_off:
        if minutes_to_pickup <= 20:
            return ("at_risk",
                    f"Pickup in {minutes_to_pickup:.0f} min, vehicle not moving")
        return ("watch",
                f"Pickup in {minutes_to_pickup:.0f} min, vehicle not moving yet")

    # 7. Slack-based bands.
    #    Tunable; start conservative for MCO/Port pickups.
    if minutes_to_pickup < 0 and leg.status not in {"picked-up", "on-location"}:
        return ("late",
                f"Past scheduled pickup by {-minutes_to_pickup:.0f} min")
    if slack < 0:
        return ("at_risk",
                f"ETA {drive_min:.0f} min vs pickup in {minutes_to_pickup:.0f} min")
    if slack < 10:
        return ("watch",
                f"Only {slack:.0f} min slack to pickup")
    return ("normal", "")
Risk levels (used to color UI):

normal — no action.
watch — yellow. Dispatcher should glance.
at_risk — orange. Dispatcher should intervene (call driver / consider reassign).
late — red. Customer impact likely already happening.
unknown — grey. Used only when a vehicle is in Samsara but telemetry is stale (>15 min without an update). Means "we should know but don't" — actionable signal that telematics are broken.
No badge at all — vehicle is not in Samsara (un-onboarded in-house or affiliate). Dispatcher continues with the existing manual workflow.
Cadence: evaluated on page render for visible legs (Phase 2). A future optimization is a 60-second background sweep that writes dispatch_risk_status and pushes ntfy alerts when a leg first crosses into at_risk/late. Don't build the sweep until dispatchers ask.

Edge cases the doc must call out:

Flight delay narrows or widens the window — recompute every render.
Driver app reports picked-up (LegStatus) — short-circuit, return normal.
Vehicle assigned mid-day — DriverVehicleAssignment.date == leg.pickup_date lookup.
Multiple legs back-to-back same vehicle — only the next-upcoming leg gets risk-evaluated.
13. Phased Implementation Roadmap
Phase 0 — Audit / Setup (this document)
Goal: Agreement on scope and architecture.
Scope: This audit. Account-owner work outside the codebase: Samsara plan confirmation, API token issuance, list of vehicles in Samsara, list of FleetVehicle.vehicle_number ↔ samsara_vehicle_id mappings.
Files touched: docs/samsara_integration_audit.md only.
Risk: none.
Acceptance: Owner confirms scope; mapping list in hand; SAMSARA_API_TOKEN available in dev .env.
Phase 1 — Read-only live vehicle visibility
Goal: Dispatch can see where every in-house vehicle is, right now, on every page load.
Scope: Add Phase 1 fields to FleetVehicle. Create dispatching/samsara_service.py. Add poller (extend or sibling of the GHL scheduler) every 2–5 min. Add management command samsara_sync_vehicles. Render text-only "📍 near X • 2m ago" on dispatch dashboard in-house driver card and reservation detail page.
Files likely touched:
drivers/models.py:469 (FleetVehicle fields)
migration in drivers/migrations/
new dispatching/samsara_service.py
new dispatching/management/commands/samsara_sync_vehicles.py
either extend ghl_integration/scheduler.py or add dispatching/samsara_scheduler.py + AppConfig wiring
dispatching/templates/dispatching/legs_filter.html (driver card additions)
dispatching/templates/dispatching/reservation_view.html (per-leg vehicle line)
business/settings.py (SAMSARA_API_TOKEN, base URL)
Risk: low. All additive, no behavior change, no automated actions.
Acceptance:
Dispatcher loads dashboard and sees "📍 near MCO • 2m ago" for at least one onboarded in-house vehicle.
Legs whose vehicle has no samsara_vehicle_id (un-onboarded fleet or affiliate) render exactly as they do today — no new lines, no grey warnings.
Telemetry endpoint failure does not break the dashboard.
Polling logs "Synced N vehicles" every interval; on rate-limit, logs warning, keeps going.
python manage.py samsara_sync_vehicles produces sensible output and, with --list-mappings, prints un-mapped FleetVehicles next to available Samsara vehicles so the owner can extend coverage incrementally.
Phase 2 — ETA + late-risk badges
Goal: Dispatcher sees an at-risk badge on the dashboard before the guest notices.
Scope: Add Phase 2 fields to Leg. Create dispatching/samsara_risk.py with evaluate_leg_risk(leg) (per Sec. 12). Compute risk at request time for visible legs with 60 s cache. Render badge in legs_filter.html and risk banner in reservation_view.html.
Files likely touched:
reservations/models.py (Leg fields)
migration in reservations/migrations/
new dispatching/samsara_risk.py
dispatching/views.py index() and reservation_details()
templates above
Risk: medium. Risk thresholds need tuning against real traffic; start conservative.
Acceptance:
A test leg with vehicle 60 min away and pickup in 30 min renders an orange "at_risk" badge with reason text.
completed, cancelled, affiliate legs never show colored badges.
Toggling a FleetVehicle.samsara_last_seen_at to 30 min stale produces a grey "unknown" badge.
Phase 3 — Route progress (optional, gated on owner request)
Goal: Use Samsara routes to enrich status transitions.
Scope: Add samsara_route_id to Leg. On driver assign, optionally create a Samsara route from pickup→dropoff. Render Samsara-reported status as a suggestion alongside manual dropdown.
Risk: medium-high (competes with manual status truth).
Acceptance: dispatcher can opt-in per leg; no automated overwrites.
Phase 4 — Webhooks + geofences
Goal: Auto-detect arrivals at MCO / Port / warehouse.
Scope: Define geofences in Samsara console (operational task, not code). Add /integrations/samsara/webhook/ endpoint with signature verification. Wire events to update dispatch_risk_status (and optionally suggest status flips).
Files likely touched: dispatching/samsara_webhooks.py, dispatching/urls.py, business/urls.py mount.
Risk: medium (signature verification details, idempotency).
Acceptance: vehicle entering MCO geofence produces a log entry within 5 s.
Phase 5 — Customer live-tracking links
Goal: Send the guest a link instead of fielding "where is my driver?" calls.
Scope: Add live_tracking_url to Leg. Generate via Samsara Live Sharing API when driver hits on-the-way. Manual send button in dispatch UI; SMS via existing Twilio path.
Files likely touched: dispatching/samsara_tracking.py, reservation detail template, dispatching/confirmation_sms.py.
Risk: medium (privacy considerations; only generate links for legs with consenting guests / specific trip types).
Acceptance: dispatcher clicks "Send tracking link" → guest SMS delivered; link expires per Samsara settings; not auto-sent.
Phase 6 — Vehicle readiness
Goal: Catch "vehicle has open DVIR / is low fuel" before assignment.
Scope: Sync samsara_open_dvir_count and samsara_fuel_percent onto FleetVehicle via the same poller. Render warnings in the in-house vehicle assignment modal. No assignment blocking.
Risk: low (additive).
Acceptance: Vehicle with DVIR shows warning chip in assignment UI.
14. Open Questions
Owner is the Samsara account holder, so API/plan questions are quick lookups, not blockers. The remaining real questions are operational:

Polling cadence — start at 3 min; verify against Samsara rate limits and account size after 1 week of Phase 1.
Risk thresholds — current pseudocode uses 10/20/45 min cutoffs. Tune after the first 2 weeks of live data with real MCO and Port runs.
Buffer minutes for flight-driven pickups — assumed 30 min after gate arrival for bags + walk. Owner to confirm what number reflects reality at MCO and Port.
Onboarding cadence — at what point do we declare "enough of the fleet is in Samsara that we can ship Phase 2"? (Risk engine works leg-by-leg, so technically any coverage is fine, but a banner that only ever lights up for 30% of trips is less useful.)
FleetVehicle ↔ Samsara mapping mechanism — manual via Django admin (simplest) vs auto-match-then-confirm by vehicle_number or VIN. Recommend manual + a management-command listing helper for Phase 1.
Customer Live Sharing consent (Phase 5) — explicit opt-in on the booking form, or implied for premium service? Product decision.
Drive-time source for risk engine — confirmed Google Maps Distance Matrix for Phase 2 (already wired). Revisit if Samsara native ETA proves cheaper/better in Phase 3.
15. Things Not To Implement Yet
No Samsara route creation until Phase 3 — manual dispatch remains the source of truth.
No automatic status transitions based on Samsara events — they only suggest.
No customer-facing tracking links until Phase 5, and even then only with manual send first.
No DVIR/fuel data sync until Phase 6.
No webhook endpoint until Phase 4 — polling first.
No map UI in Phase 1; revisit only after Phase 2 ships.
No Driver.samsara_driver_id until there's a concrete use case (today the FleetVehicle → assignment → leg chain is enough).
No SamsaraVehicleSnapshot history table — Samsara is the system of record for telemetry; we only store latest values.
No Celery / no separate worker dyno — extend the existing in-process scheduler instead.
No Sentry-style alerting integration in scope here.
No changes to existing models, views, or templates in Phase 0 (this audit).

---

## Verification (after the file exists)

- The file is the only filesystem change; `git status` should show exactly
  one new untracked file: `docs/samsara_integration_audit.md`.
- Every file link in the doc (e.g., `[reservations/models.py:841]`)
  resolves; spot-check 5–6.
- No code, settings, migrations, or templates changed.
- Doc compiles in any markdown viewer (table syntax, code fences).
- The owner can take the doc directly to a Samsara account-rep call to
  confirm the open questions in Sec. 14.

## Next step

Call ExitPlanMode to request approval. On approval I will create
`docs/samsara_integration_audit.md` with the content above and report back
with the post-work summary (file created, top 5 opportunities, Phase 1
scope, blockers — per `<final_response_after_work>`).
```
