import os
import sys
import logging

from django.apps import AppConfig
from django.conf import settings


logger = logging.getLogger(__name__)


class SamsaraIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "samsara_integration"

    def ready(self):
        # Same gating as ghl_integration.apps: skip in management commands,
        # only run in the runserver child or under gunicorn.
        running_management = any(
            cmd in sys.argv for cmd in ["migrate", "makemigrations", "collectstatic", "shell", "test", "check"]
        )
        if running_management:
            return

        is_runserver = "runserver" in sys.argv
        is_gunicorn = "gunicorn" in sys.modules

        if is_runserver:
            if os.environ.get("RUN_MAIN") != "true":
                return
        elif not is_gunicorn:
            return

        # Extra Samsara-specific gates: stays off until devices arrive and ops flips it on.
        if not getattr(settings, "SAMSARA_SYNC_ENABLED", False):
            logger.info("Samsara sync disabled (SAMSARA_SYNC_ENABLED=False)")
            return
        if not getattr(settings, "SAMSARA_API_KEY", ""):
            logger.warning("Samsara sync enabled but SAMSARA_API_KEY is empty — not starting daemon")
            return

        from samsara_integration.scheduler import start_scheduler
        start_scheduler()
