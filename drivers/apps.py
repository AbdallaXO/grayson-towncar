import os
from django.apps import AppConfig


class DriversConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "drivers"

    def ready(self):
        import drivers.signals

        # Start the early-morning wake-up sweeper. Only in the main serving
        # process, never in management commands. Mirrors the guard in
        # dispatching/apps.py (Samsara poller).
        import sys

        running_management = any(
            cmd in sys.argv
            for cmd in ["migrate", "makemigrations", "collectstatic", "shell", "test", "check"]
        )
        if running_management:
            return

        is_runserver = "runserver" in sys.argv
        is_gunicorn = "gunicorn" in sys.modules

        if is_runserver:
            # Only the reloader child serves requests (RUN_MAIN='true').
            if os.environ.get("RUN_MAIN") != "true":
                return
        elif is_gunicorn:
            # Production web workers do NOT start the wake-up sweeper. Each gunicorn
            # worker calls ready(), so starting here ran it in triplicate (one per
            # worker), multiplying background threads and DB connections
            # (connection-saturation outage 2026-07-18). It now runs as ONE
            # dedicated process via `manage.py run_schedulers` (Railway worker
            # service). Escape hatch: RUN_SCHEDULERS_IN_WEB=1.
            if os.environ.get("RUN_SCHEDULERS_IN_WEB") != "1":
                return
        else:
            # Unknown context (incl. `manage.py run_schedulers`, which starts it
            # explicitly) — don't auto-start here.
            return

        from drivers.wakeup_scheduler import start_wakeup_scheduler
        start_wakeup_scheduler()
