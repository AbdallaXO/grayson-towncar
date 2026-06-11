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
        elif not is_gunicorn:
            # Unknown context — don't start.
            return

        from drivers.wakeup_scheduler import start_wakeup_scheduler
        start_wakeup_scheduler()
