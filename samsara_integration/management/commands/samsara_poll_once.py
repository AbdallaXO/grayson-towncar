"""
Pull current stats for every mapped FleetVehicle and upsert
SamsaraVehicleSnapshot rows.

Usage:
    python manage.py samsara_poll_once

Same logic the daemon will run; calling this directly lets you debug the
poll path without flipping SAMSARA_SYNC_ENABLED. Safe pre-devices — exits
cleanly with "(no mapped vehicles)" if nothing has a samsara_vehicle_id yet.
"""

import sys
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from drivers.models import FleetVehicle
from samsara_integration.client import SamsaraClient, SamsaraError
from samsara_integration.models import SamsaraVehicleSnapshot


METERS_PER_MILE = Decimal("1609.344")
STAT_TYPES = ["gps", "fuelPercents", "obdOdometerMeters", "engineStates"]


class Command(BaseCommand):
    help = "Poll Samsara stats once and upsert SamsaraVehicleSnapshot rows."

    def handle(self, *args, **options):
        mapped = list(FleetVehicle.objects.filter(
            samsara_sync_enabled=True,
        ).exclude(samsara_vehicle_id=""))

        if not mapped:
            self.stdout.write("(no mapped vehicles - run samsara_sync_vehicles --apply first)")
            return

        by_samsara_id = {fv.samsara_vehicle_id: fv for fv in mapped}
        ids = list(by_samsara_id.keys())

        client = SamsaraClient()
        try:
            stats = client.get_vehicle_stats(ids, STAT_TYPES)
        except SamsaraError as e:
            self.stderr.write(self.style.ERROR(f"samsara error: {e}"))
            sys.exit(1)

        updated = 0
        for entry in stats:
            sid = str(entry.get("id") or "")
            fv = by_samsara_id.get(sid)
            if not fv:
                continue

            fields = self._extract(entry)
            SamsaraVehicleSnapshot.objects.update_or_create(
                fleet_vehicle=fv,
                defaults=fields,
            )
            FleetVehicle.objects.filter(pk=fv.pk).update(samsara_last_synced_at=timezone.now())
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"snapshot upserted for {updated} of {len(mapped)} mapped vehicle(s)"
        ))

    @staticmethod
    def _extract(entry):
        """Turn one Samsara stats payload into SamsaraVehicleSnapshot field kwargs."""
        out = {}
        gps = entry.get("gps") or {}
        if gps:
            out["latitude"] = gps.get("latitude")
            out["longitude"] = gps.get("longitude")
            out["speed_mph"] = gps.get("speedMilesPerHour")
            out["heading_degrees"] = gps.get("headingDegrees")
            out["formatted_address"] = (gps.get("reverseGeo") or {}).get("formattedLocation", "")
            ts = gps.get("time")
            if ts:
                out["location_recorded_at"] = parse_datetime(ts)

        fuel = entry.get("fuelPercents") or {}
        if fuel and fuel.get("value") is not None:
            out["fuel_percent"] = int(fuel["value"])

        odo = entry.get("obdOdometerMeters") or {}
        if odo and odo.get("value") is not None:
            try:
                out["odometer_miles"] = int(Decimal(str(odo["value"])) / METERS_PER_MILE)
            except (ValueError, TypeError):
                pass

        engine = entry.get("engineStates") or {}
        if engine and engine.get("value"):
            out["engine_state"] = str(engine["value"])[:20]

        return out
