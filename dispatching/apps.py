import os
from django.apps import AppConfig


class DispatchingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dispatching"

    def ready(self):
        # Sandbox tripwire: alarm on live Leg.driver writes that bypass the
        # assignment front door while a day is held. Connected in EVERY
        # process type — tests are exactly where it must be active.
        from dispatching.assignment import install_tripwire
        install_tripwire()

        # Start the Samsara live-position poller. Only in the main serving
        # process, never in management commands. Mirrors the guard in
        # ghl_integration/apps.py.
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
            # With the auto-reloader two processes run and only the child
            # (RUN_MAIN='true') serves requests. With --noreload there is a
            # single serving process and RUN_MAIN is never set — start directly.
            if "--noreload" not in sys.argv and os.environ.get("RUN_MAIN") != "true":
                return
        elif not is_gunicorn:
            # Unknown context — don't start.
            return

        from dispatching.samsara_scheduler import start_samsara_scheduler
        start_samsara_scheduler()
