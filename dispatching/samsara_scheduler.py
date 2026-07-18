"""
Background poller for Samsara live vehicle positions (Phase 1).

Modeled on ghl_integration/scheduler.py — a single daemon thread inside the
Django/Gunicorn process, guarded by a PostgreSQL advisory lock so only ONE
worker actually polls each cycle. No Celery, no separate worker dyno.

The render path NEVER calls Samsara: this loop writes the latest GPS snapshot
onto FleetVehicle.samsara_* columns, and the dashboard/reservation pages read
those columns. (Learned from the 2026-05-31 live-distance worker-timeout.)

Inert until configured: if SAMSARA_API_TOKEN is empty, or no FleetVehicle has a
samsara_vehicle_id, a cycle is a no-op.

Started from DispatchingConfig.ready() in apps.py.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_scheduler_started = False
_lock = threading.Lock()

# How often we refresh live vehicle positions (free Samsara GPS poll).
INTERVAL_SECONDS = 3 * 60  # 3 minutes
# How often we recompute the PAID Google drive-time ETAs. Decoupled from GPS so a
# parked/slow-moving fleet doesn't pay every 3 min. The free slack/band math still
# re-runs every cycle (see sweep_eta), so risk badges stay live; only the dollar call
# is throttled.
ETA_REFRESH_SECONDS = 6 * 60  # 6 minutes
# monotonic timestamp of the last cycle that was allowed to hit Google. 0.0 => first
# cycle always refreshes.
_last_eta_refresh_at = 0.0

# Advisory lock ID — MUST differ from ghl_integration's 737_201.
_SAMSARA_LOCK_ID = 737_202  # "GTC samsara poller"


def _try_advisory_lock() -> bool:
    """
    Acquire a PostgreSQL session-level advisory lock so only one worker polls.
    Returns True unconditionally on SQLite (dev — single process).
    """
    from django.db import connection

    if connection.vendor != "postgresql":
        return True

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_SAMSARA_LOCK_ID])
        return cursor.fetchone()[0]


def sync_vehicles() -> dict:
    """
    One poll cycle: fetch GPS for every mapped FleetVehicle and write the latest
    snapshot. Returns a small summary dict (also handy for the management command).
    Never raises — logs and returns on any failure.
    """
    from django.utils import timezone
    from drivers.models import FleetVehicle
    from dispatching.samsara_service import SamsaraService, parse_gps_record

    service = SamsaraService()
    if not service.is_configured():
        return {"status": "skipped", "reason": "no_token", "updated": 0}

    mapped = list(FleetVehicle.objects.exclude(samsara_vehicle_id=""))
    if not mapped:
        return {"status": "skipped", "reason": "no_mapped_vehicles", "updated": 0}

    by_samsara_id = {v.samsara_vehicle_id: v for v in mapped}
    result = service.get_vehicle_stats(
        vehicle_ids=list(by_samsara_id.keys()), types=("gps",)
    )
    if result.get("status") != "success":
        logger.warning(f"Samsara sync skipped: {result.get('status')} ({result.get('error')})")
        return {"status": result.get("status"), "updated": 0, "error": result.get("error")}

    now = timezone.now()
    to_update = []
    for record in result.get("data", []):
        vehicle = by_samsara_id.get(str(record.get("id")))
        if vehicle is None:
            continue
        fields = parse_gps_record(record)
        if fields is None:
            continue
        # Track when the vehicle last stopped moving (for dwell detection): clear
        # while driving; stamp the moment it first reads stationary. Compare to the
        # OLD movement value before we overwrite it below.
        new_movement = fields.get("samsara_movement_status") or ""
        sample_time = fields.get("samsara_last_seen_at") or now
        if new_movement == "driving":
            vehicle.samsara_stationary_since = None
        elif new_movement in ("idle", "off"):
            if vehicle.samsara_stationary_since is None or vehicle.samsara_movement_status == "driving":
                vehicle.samsara_stationary_since = sample_time
        # (unknown/blank movement -> leave samsara_stationary_since unchanged)
        for attr, value in fields.items():
            setattr(vehicle, attr, value)
        vehicle.samsara_last_synced_at = now
        to_update.append(vehicle)

    if to_update:
        FleetVehicle.objects.bulk_update(
            to_update,
            [
                "samsara_last_latitude", "samsara_last_longitude",
                "samsara_last_location_label", "samsara_movement_status",
                "samsara_last_seen_at", "samsara_last_synced_at",
                "samsara_stationary_since",
            ],
        )
    logger.info(f"Samsara: synced {len(to_update)} vehicle(s)")
    return {"status": "success", "updated": len(to_update)}


_ETA_FIELDS = [
    "dispatch_eta_minutes", "dispatch_eta_target", "dispatch_eta_target_time",
    "dispatch_risk_status", "dispatch_risk_reason", "dispatch_eta_evaluated_at",
    "dispatch_is_moving", "dispatch_stationary_minutes", "dispatch_vehicle_label",
    "dispatch_eta_origin_lat", "dispatch_eta_origin_lng", "dispatch_eta_origin_target",
]


def _apply_eta_fields(leg, result, now):
    """Set the active leg's dispatch_* fields from an evaluate() result. Always
    refreshes evaluated_at so freshness advances each cycle."""
    values = {**result, "dispatch_eta_evaluated_at": now}
    for field, value in values.items():
        setattr(leg, field, value)
    return True


def _clear_eta_fields(leg):
    """Clear dispatch_* on a non-active leg. Returns False if already clear (so we
    don't write rows that haven't changed)."""
    if (leg.dispatch_eta_evaluated_at is None and not leg.dispatch_risk_status
            and leg.dispatch_eta_minutes is None and not leg.dispatch_eta_target):
        return False
    leg.dispatch_eta_minutes = None
    leg.dispatch_eta_target = ""
    leg.dispatch_eta_target_time = None
    leg.dispatch_risk_status = ""
    leg.dispatch_risk_reason = ""
    leg.dispatch_eta_evaluated_at = None
    leg.dispatch_is_moving = None
    leg.dispatch_stationary_minutes = None
    leg.dispatch_vehicle_label = ""
    leg.dispatch_eta_origin_lat = None
    leg.dispatch_eta_origin_lng = None
    leg.dispatch_eta_origin_target = ""
    return True


def sweep_eta(now=None, refresh_eta=True) -> dict:
    """
    For each in-house driver with legs today, compute the schedule-aware ETA +
    late-risk for their single next stop and persist it on that leg; clear any
    previously-flagged sibling legs. Only the next stop carries a badge.
    Never raises — logs and returns a summary. `now` is injectable for tests.

    `refresh_eta` (Lever 4 cadence gate): when False, the PAID Google drive-time
    lookups are skipped and stored ETAs are reused — only the free slack/band math
    re-runs against the clock. GPS freshness is unaffected (that's sync_vehicles()).
    Defaults True so management commands / manual runs always refresh.
    """
    from django.utils import timezone
    from reservations.models import Leg
    from dispatching.samsara_service import resolve_assigned_fleet_vehicle
    from dispatching.samsara_risk import evaluate_driver

    now = now or timezone.now()
    today = timezone.localdate(now)
    legs = list(
        Leg.objects.filter(pickup_date=today, driver__driver_type="inhouse")
        .exclude(status__in=["completed", "cancelled"])
        .select_related("driver", "flight_information")
        .prefetch_related("legflight_set__flight")
        .order_by("driver_id", "pickup_time")
    )
    if not legs:
        return {"status": "ok", "drivers": 0, "flagged": 0}

    by_driver = {}
    for leg in legs:
        by_driver.setdefault(leg.driver_id, []).append(leg)

    to_update = []
    flagged = 0
    for dlegs in by_driver.values():
        vehicle = resolve_assigned_fleet_vehicle(dlegs[0])
        results = evaluate_driver(vehicle, dlegs, now, refresh_allowed=refresh_eta)  # {leg_id: fields}
        for leg in dlegs:
            fields = results.get(leg.id)
            if fields is not None:
                if _apply_eta_fields(leg, fields, now):
                    to_update.append(leg)
                flagged += 1
            elif _clear_eta_fields(leg):
                to_update.append(leg)

    if to_update:
        Leg.objects.bulk_update(to_update, _ETA_FIELDS)
    logger.info(f"Samsara ETA sweep: flagged {flagged} leg(s) across {len(by_driver)} driver(s)")
    return {"status": "ok", "drivers": len(by_driver), "flagged": flagged}


def _run_scheduler():
    """Daemon loop. Dies with the process. Survives any per-cycle exception."""
    time.sleep(60)  # let Django finish booting
    logger.info(f"Samsara poller started (interval: {INTERVAL_SECONDS}s)")
    global _last_eta_refresh_at
    while True:
        acquired = False
        try:
            acquired = _try_advisory_lock()
            if acquired:
                sync_vehicles()  # free GPS poll, every cycle
                # Throttle the paid Google ETA recompute to ETA_REFRESH_SECONDS; the
                # band math inside sweep_eta still runs every cycle either way.
                now_mono = time.monotonic()
                refresh = (now_mono - _last_eta_refresh_at) >= ETA_REFRESH_SECONDS
                sweep_eta(refresh_eta=refresh)
                if refresh:
                    _last_eta_refresh_at = now_mono
            else:
                logger.debug("Another worker holds the Samsara poller lock, skipping cycle")
        except Exception as e:
            logger.error(f"Samsara poller error: {e}", exc_info=True)
        finally:
            # Don't pin a Postgres connection idle across the sleep (connection
            # saturation, incident 2026-07-18). Only the leader keeps its
            # connection — the session advisory lock lives on it, so closing it
            # would drop the lock and let another worker double-run the cycle.
            # Non-leaders (and any error/reconnect case) release + reconnect next
            # cycle.
            if not acquired:
                from django.db import connections
                connections.close_all()
        time.sleep(INTERVAL_SECONDS)


def start_samsara_scheduler():
    """Start the poller once per process. Safe to call multiple times."""
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    thread = threading.Thread(target=_run_scheduler, daemon=True, name="samsara-poller")
    thread.start()
    logger.info("Samsara poller thread spawned")
