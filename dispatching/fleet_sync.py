"""
Fleet Management persistence: daily mileage accrual, master refresh, fault
episodes, and the nightly reconcile.

Where this runs
---------------
Inside the EXISTING 3-minute Samsara poller thread (dispatching/samsara_scheduler),
under the same advisory lock 737_202. There is no cron in this repo — no Procfile,
no Railway cron, no Celery (django_celery_beat is installed but nothing imports
celery) — so "nightly" is expressed as a self-gating call that no-ops until the
local clock is in the window and today's run hasn't happened yet.

Nothing here may raise. The same leader thread runs the ETA sweep the dispatch
board depends on, and railway.json sets restartPolicyMaxRetries=10 — a crash loop
in background work can burn the restart budget and take the web service down.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from dispatching.mileage import (
    SOURCE_NONE,
    OdometerReading,
    meters_to_miles,
    resolve_day_mileage,
)

logger = logging.getLogger(__name__)

# Local-clock window the nightly reconcile is allowed to run in. Wide on purpose:
# the poller restarts with a 60-second boot sleep on every gunicorn worker recycle
# (--max-requests 1500 makes those routine), so a narrow window could be missed.
# At a 3-minute cadence this window offers ~60 attempts; the durable stamp makes
# all but the first a no-op.
RECONCILE_HOUR_START = 3
RECONCILE_HOUR_END = 6

# A day with no sample for longer than this is under-counted. Flagged rather than
# silently averaged, so a sparse day looks sparse.
GAP_MINUTES = 90

FEED_NIGHTLY = "nightly_reconcile"
FEED_VEHICLE_STATS = "vehicle_stats"
FEED_FAULTS = "faults"


# ════════════════════════════════════════════════════════════════════════════
# Daily mileage accrual — runs every poll cycle
# ════════════════════════════════════════════════════════════════════════════

def accrue_vehicle_day(now=None) -> dict:
    """
    Upsert today's VehicleDayReading for every mapped vehicle carrying telemetry.

    Day boundaries are LOCAL (America/New_York, USE_TZ=True). A naive UTC date
    would push the 8pm-to-midnight window onto the next day, which for an airport
    operation is a meaningful chunk of the work.

    Contiguity: a day's mileage is measured against the PREVIOUS day's closing
    reading, not against the first sample seen today. Otherwise every mile driven
    between last-sample-yesterday and first-sample-today silently vanishes — and
    an overnight MCO run is exactly that shape.

    Idempotent: `miles_driven` is recomputed from the stored start/end on every
    call, never accumulated. Running this twice produces the same row.
    """
    from drivers.models import FleetVehicle, VehicleDayReading

    now = now or timezone.now()
    today = timezone.localdate(now)
    yesterday = today - timedelta(days=1)

    vehicles = list(
        FleetVehicle.objects.exclude(samsara_vehicle_id="")
        .filter(is_active=True)
        .only(
            "id", "samsara_vehicle_id", "samsara_odometer_meters",
            "samsara_gps_distance_meters", "samsara_odometer_at",
            "samsara_last_seen_at",
        )
    )
    if not vehicles:
        return {"status": "skipped", "reason": "no_mapped_vehicles", "rows": 0}

    ids = [v.id for v in vehicles]
    today_rows = {
        r.vehicle_id: r
        for r in VehicleDayReading.objects.filter(vehicle_id__in=ids, date=today)
    }
    prior_rows = {
        r.vehicle_id: r
        for r in VehicleDayReading.objects.filter(vehicle_id__in=ids, date=yesterday)
    }

    to_create, to_update = [], []
    for vehicle in vehicles:
        odo = vehicle.samsara_odometer_meters
        dist = vehicle.samsara_gps_distance_meters
        if odo is None and dist is None:
            continue  # GPS-only gateway with no distance counter: nothing to record

        sample_at = vehicle.samsara_odometer_at or vehicle.samsara_last_seen_at or now
        row = today_rows.get(vehicle.id)

        if row is None:
            prior = prior_rows.get(vehicle.id)
            row = VehicleDayReading(
                vehicle_id=vehicle.id,
                date=today,
                samsara_vehicle_id=vehicle.samsara_vehicle_id,
                # Opening reading = yesterday's close, so no miles fall in the
                # gap between the last poll of one day and the first of the next.
                start_odometer_meters=prior.end_odometer_meters if prior else None,
                start_gps_distance_meters=prior.end_gps_distance_meters if prior else None,
                first_sample_at=sample_at,
                sample_count=0,
            )
            to_create.append(row)
        else:
            to_update.append(row)

        # A gateway swap mid-day makes the whole day's arithmetic unsafe; record
        # which id closed the day so the resolver can refuse to diff across it.
        row.samsara_vehicle_id = vehicle.samsara_vehicle_id
        row.end_odometer_meters = odo
        row.end_gps_distance_meters = dist
        row.sample_count = (row.sample_count or 0) + 1
        if row.last_sample_at and sample_at:
            gap = (sample_at - row.last_sample_at).total_seconds() / 60
            if gap > GAP_MINUTES:
                row.has_gap = True
        row.last_sample_at = sample_at

        _recompute_row_mileage(row, prior_rows.get(vehicle.id))

    fields = [
        "samsara_vehicle_id", "end_odometer_meters", "end_gps_distance_meters",
        "sample_count", "last_sample_at", "has_gap", "miles_driven",
        "mileage_source", "mileage_note", "start_odometer_meters",
        "start_gps_distance_meters",
    ]
    with transaction.atomic():
        if to_create:
            # ignore_conflicts: two workers racing the same cycle must not raise
            # on the (vehicle, date) unique. The loser's values are identical.
            VehicleDayReading.objects.bulk_create(to_create, ignore_conflicts=True)
        if to_update:
            VehicleDayReading.objects.bulk_update(to_update, fields)

    return {
        "status": "success",
        "rows": len(to_create) + len(to_update),
        "created": len(to_create),
    }


def _recompute_row_mileage(row, prior_row):
    """
    Set miles_driven/mileage_source/mileage_note from the row's own start/end.

    Pure re-derivation — call it as often as you like. `prior_row` supplies the
    gateway id the opening reading came from, so a mid-series gateway swap is
    refused rather than turned into a fictional six-figure day.
    """
    opening_vid = (prior_row.samsara_vehicle_id if prior_row else row.samsara_vehicle_id) or ""

    previous = None
    if row.start_odometer_meters is not None or row.start_gps_distance_meters is not None:
        previous = OdometerReading(
            samsara_vehicle_id=opening_vid,
            obd_odometer_meters=row.start_odometer_meters,
            gps_distance_meters=row.start_gps_distance_meters,
        )
    current = OdometerReading(
        samsara_vehicle_id=row.samsara_vehicle_id or "",
        obd_odometer_meters=row.end_odometer_meters,
        gps_distance_meters=row.end_gps_distance_meters,
    )

    result = resolve_day_mileage(previous, current)
    row.miles_driven = meters_to_miles(result.meters)
    row.mileage_source = result.source or SOURCE_NONE
    row.mileage_note = (result.note or "")[:200]


# ════════════════════════════════════════════════════════════════════════════
# Vehicle master refresh — VIN / plate / Samsara name
# ════════════════════════════════════════════════════════════════════════════

def refresh_vehicle_master(service=None) -> dict:
    """
    Pull /fleet/vehicles and fill in VIN, plate and Samsara's own label.

    The VIN is not decoration. samsara_vehicle_id is a mutable pointer with no
    history — the poller writes via bulk_update, which bypasses save() and
    signals — so a gateway moved between cars would silently re-attribute all
    history with no trace. The VIN under a stable id changing is the detector,
    and it writes an AuditLog row plus a loud warning.
    """
    from django.db.models import Q
    from drivers.models import FleetVehicle
    from reservations.models import AuditLog
    from dispatching.samsara_service import SamsaraService

    service = service or SamsaraService()
    if not service.is_configured():
        return {"status": "skipped", "reason": "no_token", "updated": 0}

    result = service.list_vehicles()
    if result.get("status") != "success":
        logger.warning(f"Fleet master refresh failed: {result.get('error')}")
        return {"status": result.get("status"), "updated": 0, "error": result.get("error")}

    by_id = {
        v.samsara_vehicle_id: v
        for v in FleetVehicle.objects.exclude(samsara_vehicle_id="")
    }
    updated, drifted = [], 0

    for sv in result.get("data", []):
        vehicle = by_id.get(str(sv.get("id", "")))
        if vehicle is None:
            continue

        vin = (sv.get("vin") or "").strip().upper()[:17]
        plate = (sv.get("licensePlate") or "").strip()[:16]
        name = (sv.get("name") or "").strip()[:128]
        changed = False

        if vin and vehicle.vin and vehicle.vin != vin:
            # THE gateway-swap signal. Loud, audited, and never auto-corrected —
            # a human decides whether the car or the mapping is wrong.
            drifted += 1
            logger.error(
                "SAMSARA VIN DRIFT on vehicle #%s (samsara id %s): stored VIN %s "
                "but Samsara now reports %s. A gateway may have moved between "
                "cars — every mileage row keyed on this id is suspect until "
                "resolved. Not auto-corrected.",
                vehicle.vehicle_number, vehicle.samsara_vehicle_id, vehicle.vin, vin,
            )
            AuditLog.objects.create(
                model_name="FleetVehicle", object_id=vehicle.id, action="updated",
                field_name="vin", old_value=vehicle.vin, new_value=vin,
                notes=(
                    f"Samsara VIN drift under stable samsara_vehicle_id "
                    f"{vehicle.samsara_vehicle_id}. Possible gateway swap. "
                    f"Mileage history for this vehicle is suspect."
                ),
            )
        elif vin and not vehicle.vin:
            vehicle.vin = vin
            changed = True

        if plate and vehicle.license_plate != plate:
            vehicle.license_plate = plate
            changed = True
        if name and vehicle.samsara_name != name:
            vehicle.samsara_name = name
            changed = True

        if changed:
            updated.append(vehicle)

    if updated:
        FleetVehicle.objects.bulk_update(updated, ["vin", "license_plate", "samsara_name"])

    logger.info(f"Fleet master: updated {len(updated)} vehicle(s), {drifted} VIN drift(s)")
    return {"status": "success", "updated": len(updated), "vin_drift": drifted}


# ════════════════════════════════════════════════════════════════════════════
# Nightly reconcile
# ════════════════════════════════════════════════════════════════════════════

def should_reconcile(now=None) -> bool:
    """
    True when the local clock is in the window AND today's reconcile hasn't run.

    The stamp is DB-persisted rather than an in-memory counter. The one existing
    "run less often" mechanism in this repo (ghl_integration's _cycle_count % N)
    resets on every worker recycle, and with --max-requests 1500 those are
    routine — an in-memory gate would fire many times a day or not at all.
    """
    from drivers.models import FleetSyncState

    now = now or timezone.now()
    local = timezone.localtime(now)
    if not (RECONCILE_HOUR_START <= local.hour < RECONCILE_HOUR_END):
        return False

    state = FleetSyncState.objects.filter(feed=FEED_NIGHTLY).first()
    if state is None or state.last_success_at is None:
        return True
    return timezone.localtime(state.last_success_at).date() < local.date()


def reconcile_fleet(now=None, service=None) -> dict:
    """
    The nightly pass. Refreshes vehicle master and finalises yesterday's mileage
    rows. Never raises — every step is independently guarded so one failure does
    not cost the others.
    """
    from drivers.models import FleetSyncState

    now = now or timezone.now()
    summary = {"status": "success", "steps": {}}

    for name, fn in (
        ("master", lambda: refresh_vehicle_master(service=service)),
        ("accrue", lambda: accrue_vehicle_day(now=now)),
        ("finalise", lambda: finalise_previous_day(now=now)),
    ):
        try:
            summary["steps"][name] = fn()
        except Exception as e:
            logger.error(f"Fleet reconcile step '{name}' failed: {e}", exc_info=True)
            summary["steps"][name] = {"status": "error", "error": str(e)}
            summary["status"] = "partial"

    record_feed_result(FEED_NIGHTLY, summary["status"], now=now)
    logger.info(f"Fleet reconcile finished: {summary['status']}")
    return summary


def finalise_previous_day(now=None) -> dict:
    """
    Re-derive mileage for the last few local days.

    Recomputes rather than patches: a day whose opening reading arrived late (or
    whose prior day was backfilled) gets the right answer on the next pass
    instead of staying wrong forever. Three days is enough to absorb a weekend
    outage without scanning history.
    """
    from drivers.models import VehicleDayReading

    now = now or timezone.now()
    today = timezone.localdate(now)
    window_start = today - timedelta(days=3)

    rows = list(
        VehicleDayReading.objects.filter(date__gte=window_start, date__lte=today)
        .order_by("vehicle_id", "date")
    )
    if not rows:
        return {"status": "success", "rows": 0}

    by_vehicle = {}
    for row in rows:
        by_vehicle.setdefault(row.vehicle_id, []).append(row)

    # The day before the window needs loading too, or its successor has no opener.
    priors = {
        r.vehicle_id: r
        for r in VehicleDayReading.objects.filter(
            vehicle_id__in=by_vehicle.keys(), date=window_start - timedelta(days=1)
        )
    }

    touched = []
    for vehicle_id, vehicle_rows in by_vehicle.items():
        prior = priors.get(vehicle_id)
        for row in vehicle_rows:
            if prior is not None and prior.date == row.date - timedelta(days=1):
                row.start_odometer_meters = prior.end_odometer_meters
                row.start_gps_distance_meters = prior.end_gps_distance_meters
            _recompute_row_mileage(row, prior)
            touched.append(row)
            prior = row

    VehicleDayReading.objects.bulk_update(
        touched,
        ["start_odometer_meters", "start_gps_distance_meters",
         "miles_driven", "mileage_source", "mileage_note"],
    )
    return {"status": "success", "rows": len(touched)}


# ════════════════════════════════════════════════════════════════════════════
# Feed health
# ════════════════════════════════════════════════════════════════════════════

def record_feed_result(feed, status, error="", now=None):
    """
    Stamp a feed's outcome. This is the surface that would have caught the ~25-day
    silent outage — the poller ran every 3 minutes and reported nothing wrong
    because nothing was watching.
    """
    from drivers.models import FleetSyncState

    now = now or timezone.now()
    state, _ = FleetSyncState.objects.get_or_create(feed=feed)
    state.last_run_at = now
    state.last_status = (status or "")[:16]
    if status == "success":
        state.last_success_at = now
        state.last_error = ""
        state.consecutive_failures = 0
    else:
        state.last_error = (error or "")[:2000]
        state.consecutive_failures += 1
    state.save(update_fields=[
        "last_run_at", "last_status", "last_success_at", "last_error",
        "consecutive_failures",
    ])
    return state
