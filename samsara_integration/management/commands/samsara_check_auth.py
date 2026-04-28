"""
Verify the Samsara API key works.

Usage:
    python manage.py samsara_check_auth

Hits GET /me. Exits 0 on success, 1 on auth failure or network error.
Safe to run pre-devices — only checks the org/token, not vehicle data.
"""

import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from samsara_integration.client import (
    SamsaraClient,
    SamsaraAuthError,
    SamsaraError,
)


class Command(BaseCommand):
    help = "Verify SAMSARA_API_KEY by calling /me."

    def handle(self, *args, **options):
        if not settings.SAMSARA_API_KEY:
            self.stderr.write(self.style.ERROR("SAMSARA_API_KEY is empty in settings/.env"))
            sys.exit(1)

        client = SamsaraClient()
        try:
            payload = client.get_org()
        except SamsaraAuthError as e:
            self.stderr.write(self.style.ERROR(f"auth failed: {e}"))
            sys.exit(1)
        except SamsaraError as e:
            self.stderr.write(self.style.ERROR(f"samsara error: {e}"))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS("samsara auth OK"))
        self.stdout.write(json.dumps(payload, indent=2, default=str))
