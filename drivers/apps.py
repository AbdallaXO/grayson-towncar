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
            # Interim: the wake-up sweeper runs in the web workers (no separate
            # service yet). Each gunicorn worker spawns the thread, but the per-cycle
            # advisory lock keeps execution single and non-leader workers release
            # their DB connection each cycle, so the steady-state cost is ~1
            # connection regardless of worker count (connection-saturation
            # 2026-07-18). To move it to a dedicated `manage.py run_schedulers`
            # process (Celery / worker service) later, set RUN_SCHEDULERS_IN_WEB=0.
            if os.environ.get("RUN_SCHEDULERS_IN_WEB", "1") != "1":
                return
        else:
            # Unknown context (incl. `manage.py run_schedulers`, which starts it
            # explicitly) — don't auto-start here.
            return

        from drivers.wakeup_scheduler import start_wakeup_scheduler
        start_wakeup_scheduler()
