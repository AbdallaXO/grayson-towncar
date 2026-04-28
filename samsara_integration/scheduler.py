"""
Lightweight background scheduler for Samsara polling.

Mirrors the pattern in ghl_integration.scheduler — a single daemon thread
guarded by a Postgres advisory lock so only one Gunicorn worker runs the
sync each cycle.

Phase 1 kickstart: tick() body is intentionally a no-op so the daemon can
start without doing anything risky. The actual snapshot/upsert logic lands
in a follow-up branch once devices are installed and we've validated the
response shapes against real data via samsara_poll_once.
"""

import logging
import threading
import time

from django.conf import settings


logger = logging.getLogger(__name__)

_scheduler_started = False
_lock = threading.Lock()

# Advisory lock id — distinct from ghl_integration (737201).
_SCHEDULER_LOCK_ID = 737_202  # "Samsara sync"


def _try_advisory_lock():
    """Acquire a session-level Postgres advisory lock. True on SQLite (dev)."""
    from django.db import connection

    if connection.vendor != "postgresql":
        return True

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_SCHEDULER_LOCK_ID])
        acquired = cursor.fetchone()[0]
    return acquired


def tick():
    """
    Per-cycle work. Keep this function callable from samsara_poll_once so the
    daemon and the management command share one code path.

    Phase 1 follow-up branch will fill this in:
        1. fetch stats for FleetVehicles with samsara_vehicle_id and an active driver today
        2. upsert SamsaraVehicleSnapshot rows
        3. every N ticks, refresh maintenance issues
    """
    logger.debug("samsara tick — no-op (kickstart scaffold)")


def _run_scheduler():
    interval = getattr(settings, "SAMSARA_POLL_INTERVAL_SECONDS", 60)
    # Wait a bit after startup so Django is fully initialized.
    time.sleep(60)
    logger.info("Samsara scheduler started (interval: %ss)", interval)

    while True:
        try:
            if _try_advisory_lock():
                tick()
            else:
                logger.debug("another worker holds the samsara lock, skipping cycle")
        except Exception:
            logger.exception("samsara scheduler tick failed")

        time.sleep(interval)


def start_scheduler():
    """Spawn the daemon thread once per process. Safe to call multiple times."""
    global _scheduler_started

    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    thread = threading.Thread(target=_run_scheduler, daemon=True, name="samsara-scheduler")
    thread.start()
    logger.info("Samsara scheduler thread spawned")
