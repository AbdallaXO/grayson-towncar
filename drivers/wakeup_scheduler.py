"""Background sweeper for early-morning driver wake-up checks.

Modeled on dispatching/samsara_scheduler.py — a single daemon thread inside
the Django/Gunicorn process, guarded by a PostgreSQL advisory lock so only
ONE worker runs the cycle. No Celery, no separate dyno.

Runs every minute because the ladder deadlines (T-90 / T-55 / T-50) are
minute-grained. A cycle is one cheap query when no early pickups are in
range, and a no-op when settings.WAKEUP_CHECKS_ENABLED is off (the thread
isn't even started then — flipping the env var requires a restart anyway).

Started from DriversConfig.ready() in apps.py.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_scheduler_started = False
_lock = threading.Lock()

INTERVAL_SECONDS = 60

# Advisory lock ID — MUST differ from ghl_integration's 737_201 and the
# Samsara poller's 737_202.
_WAKEUP_LOCK_ID = 737_203  # "GTC wakeup sweeper"


def _try_advisory_lock() -> bool:
    """Session-level advisory lock so only one worker sweeps. True on SQLite
    (dev — single process)."""
    from django.db import connection

    if connection.vendor != "postgresql":
        return True

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_WAKEUP_LOCK_ID])
        return cursor.fetchone()[0]


def _run_scheduler():
    """Daemon loop. Dies with the process. Survives any per-cycle exception."""
    from drivers.wakeup import run_wakeup_cycle

    time.sleep(60)  # let Django finish booting
    logger.info(f"Wake-up sweeper started (interval: {INTERVAL_SECONDS}s)")
    while True:
        try:
            if _try_advisory_lock():
                run_wakeup_cycle()
            else:
                logger.debug("Another worker holds the wake-up sweeper lock, skipping cycle")
        except Exception as e:
            logger.error(f"Wake-up sweeper error: {e}", exc_info=True)
        time.sleep(INTERVAL_SECONDS)


def start_wakeup_scheduler():
    """Start the sweeper once per process. Safe to call multiple times.
    No-op when the feature flag is off."""
    from django.conf import settings

    if not settings.WAKEUP_CHECKS_ENABLED:
        logger.info("Wake-up checks disabled (WAKEUP_CHECKS_ENABLED) — sweeper not started")
        return

    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    thread = threading.Thread(target=_run_scheduler, daemon=True, name="wakeup-sweeper")
    thread.start()
    logger.info("Wake-up sweeper thread spawned")
