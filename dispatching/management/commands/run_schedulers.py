"""Run all background schedulers as a SINGLE dedicated process.

Production runs the Samsara live-position poller, the GHL follow-up scheduler,
and the early-morning wake-up sweeper here instead of inside the gunicorn web
workers. Starting them from each app's ready() ran them once PER worker — with 3
workers that tripled the background threads and their Postgres connections, a
driver of the connection-saturation outage (2026-07-18).

Deploy as a Railway *worker* service (separate from the web service), same repo:
    Start command:  python manage.py run_schedulers
Because this is the only process that runs them, no cross-process locking is
needed here — the per-cycle advisory locks inside each loop remain as a backstop
in case the schedulers are ever also enabled in web (RUN_SCHEDULERS_IN_WEB=1).
"""
import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Run background schedulers (Samsara poller, GHL follow-up, wake-up "
        "sweeper) in one dedicated process. Intended for a Railway worker service."
    )

    def handle(self, *args, **options):
        # Imported lazily so `manage.py` startup (and other commands) don't spin
        # up scheduler modules.
        from ghl_integration.scheduler import start_scheduler
        from dispatching.samsara_scheduler import start_samsara_scheduler
        from drivers.wakeup_scheduler import start_wakeup_scheduler

        start_scheduler()          # GHL follow-up / flight refresh / ops tasks
        start_samsara_scheduler()  # Samsara GPS + ETA sweep
        start_wakeup_scheduler()   # early-morning driver wake-up checks (flag-gated)

        self.stdout.write(self.style.SUCCESS(
            "Background schedulers started in dedicated process; blocking."
        ))
        logger.info("run_schedulers: all background schedulers started (dedicated process)")

        # Block the main thread forever. The schedulers are daemon threads and die
        # with this process on SIGTERM (Railway redeploy).
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            logger.info("run_schedulers: shutting down")
