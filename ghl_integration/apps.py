import os
from django.apps import AppConfig


class GhlIntegrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ghl_integration'

    def ready(self):
        # Start the background scheduler for follow-up automation.
        # Only start in the main serving process, never in management commands.
        #
        # With runserver: Django's auto-reloader runs TWO processes.
        #   - Parent process: watches files, restarts child. RUN_MAIN is NOT set.
        #   - Child process: actually serves requests. RUN_MAIN='true'.
        #   We ONLY start the scheduler in the child to avoid duplicate sends.
        #
        # With Gunicorn: each worker calls ready(). We start in each worker
        #   but the tasks themselves use atomic locking to prevent duplicates.
        import sys

        running_management = any(
            cmd in sys.argv for cmd in ['migrate', 'makemigrations', 'collectstatic', 'shell', 'test', 'check']
        )
        if running_management:
            return

        is_runserver = 'runserver' in sys.argv
        is_gunicorn = 'gunicorn' in sys.modules

        if is_runserver:
            # Only start in the reloader child, not the parent watcher process
            if os.environ.get('RUN_MAIN') != 'true':
                return
        elif not is_gunicorn:
            # Unknown context — don't start
            return

        from ghl_integration.scheduler import start_scheduler
        start_scheduler()
