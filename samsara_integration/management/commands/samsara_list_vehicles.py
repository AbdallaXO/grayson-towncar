"""
Print every vehicle visible in the Samsara org.

Usage:
    python manage.py samsara_list_vehicles

Returns nothing (and exits 0) if no vehicles are registered yet — that's the
expected state pre-device-install. Use this to:
    - confirm wiring works before devices arrive
    - diff against FleetVehicle once devices are installed
"""

import sys

from django.core.management.base import BaseCommand

from samsara_integration.client import SamsaraClient, SamsaraError


class Command(BaseCommand):
    help = "List all vehicles in the Samsara organization."

    def handle(self, *args, **options):
        client = SamsaraClient()
        try:
            vehicles = client.list_vehicles()
        except SamsaraError as e:
            self.stderr.write(self.style.ERROR(f"samsara error: {e}"))
            sys.exit(1)

        if not vehicles:
            self.stdout.write("(no vehicles in Samsara - expected before devices are installed)")
            return

        self.stdout.write(self.style.SUCCESS(f"{len(vehicles)} vehicle(s):"))
        self.stdout.write(f"{'ID':<22} {'NAME':<28} {'VIN':<20} {'PLATE':<12}")
        for v in vehicles:
            self.stdout.write(
                f"{str(v.get('id', ''))[:22]:<22} "
                f"{(v.get('name') or '')[:28]:<28} "
                f"{(v.get('vin') or '')[:20]:<20} "
                f"{(v.get('licensePlate') or '')[:12]:<12}"
            )
