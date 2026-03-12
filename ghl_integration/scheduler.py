"""
Lightweight background scheduler for follow-up automation.

Replaces Celery beat — runs inside the Django/Gunicorn process.
Starts a single daemon thread that wakes up every 30 minutes to
process due follow-up tasks and batch-send unsent initial SMS.

Usage:
    Called automatically from GhlIntegrationConfig.ready() in apps.py.
    Only starts once per process (guarded by a module-level flag).
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

_scheduler_started = False
_lock = threading.Lock()
_cycle_count = 0  # Tracks how many 30-min cycles have run

# How often the scheduler runs (in seconds)
INTERVAL_SECONDS = 30 * 60  # 30 minutes


def _run_scheduler():
    """
    Background loop that runs scheduled tasks every 30 minutes.
    Runs as a daemon thread — dies when the main process exits.
    """
    global _cycle_count
    # Wait 60 seconds after startup before first run to let Django fully initialize
    time.sleep(60)
    logger.info("Follow-up scheduler started (interval: 30 minutes)")

    while True:
        _cycle_count += 1
        try:
            _run_batch_tasks()
        except Exception as e:
            logger.error(f"Scheduler error: {e}", exc_info=True)

        time.sleep(INTERVAL_SECONDS)


def _run_batch_tasks():
    """Execute the scheduled batch tasks."""
    from ghl_integration.tasks import (
        batch_send_unsent_leads, process_follow_up_batch,
        retry_failed_syncs, alert_dead_letter_syncs,
        detect_lost_leads,
    )

    # 1. Process unsent initial SMS (existing batch task)
    try:
        result = batch_send_unsent_leads()
        if result and result.get("queued", 0) > 0:
            logger.info(f"Batch SMS: queued {result['queued']} leads")
    except Exception as e:
        logger.error(f"batch_send_unsent_leads error: {e}", exc_info=True)

    # 2. Process follow-up sequence tasks
    try:
        result = process_follow_up_batch()
        if result:
            sent = result.get("sent", 0)
            cancelled = result.get("cancelled", 0)
            if sent > 0 or cancelled > 0:
                logger.info(f"Follow-up batch: {sent} sent, {cancelled} cancelled")
    except Exception as e:
        logger.error(f"process_follow_up_batch error: {e}", exc_info=True)

    # 3. Retry failed GHL sync operations (every cycle = every 30 min)
    try:
        result = retry_failed_syncs()
        if result and result.get("retried", 0) > 0:
            logger.info(f"Sync retry: {result['succeeded']} succeeded, {result['still_failed']} still failed")
    except Exception as e:
        logger.error(f"retry_failed_syncs error: {e}", exc_info=True)

    # 4. Detect lost leads (every 2 cycles = every hour)
    if _cycle_count % 2 == 0:
        try:
            result = detect_lost_leads()
            if result and result.get("lost", 0) > 0:
                logger.info(f"Lost lead detection: {result['lost']} leads marked lost")
        except Exception as e:
            logger.error(f"detect_lost_leads error: {e}", exc_info=True)

    # 5. Alert on dead letter syncs (every 12 cycles = every 6 hours)
    if _cycle_count % 12 == 0:
        try:
            result = alert_dead_letter_syncs()
            if result and result.get("dead_letters", 0) > 0:
                logger.warning(f"Dead letter alert: {result['dead_letters']} unresolved")
        except Exception as e:
            logger.error(f"alert_dead_letter_syncs error: {e}", exc_info=True)


def start_scheduler():
    """
    Start the background scheduler if not already running.
    Safe to call multiple times — only starts once per process.
    """
    global _scheduler_started

    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    thread = threading.Thread(target=_run_scheduler, daemon=True, name="followup-scheduler")
    thread.start()
    logger.info("Follow-up scheduler thread spawned")
