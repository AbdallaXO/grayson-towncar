"""
Match Samsara vehicles to FleetVehicle rows.

Usage:
    python manage.py samsara_sync_vehicles            # dry run (default)
    python manage.py samsara_sync_vehicles --apply    # actually write

Matching strategy, per Samsara vehicle, against FleetVehicles with no
samsara_vehicle_id yet:
    1. exact VIN match
    2. exact license_plate match (case-insensitive)
    3. vehicle_number == Samsara name

On a match: sets samsara_vehicle_id, fills in any blank vin/license_plate
from Samsara, and saves. Prints a summary table at the end. Anything not
matched is left for manual mapping in Django admin.
"""

import sys

from django.core.management.base import BaseCommand
from django.utils import timezone

from drivers.models import FleetVehicle
from samsara_integration.client import SamsaraClient, SamsaraError


class Command(BaseCommand):
    help = "Match Samsara vehicles to FleetVehicle rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write matches to the database. Without this, runs as a dry run.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        client = SamsaraClient()
        try:
            samsara_vehicles = client.list_vehicles()
        except SamsaraError as e:
            self.stderr.write(self.style.ERROR(f"samsara error: {e}"))
            sys.exit(1)

        if not samsara_vehicles:
            self.stdout.write("(no vehicles in Samsara - nothing to map)")
            return

        unmapped = list(FleetVehicle.objects.filter(samsara_vehicle_id=""))

        matched = []
        unmatched_samsara = []

        for sv in samsara_vehicles:
            sv_id = str(sv.get("id") or "")
            sv_vin = (sv.get("vin") or "").strip()
            sv_plate = (sv.get("licensePlate") or "").strip()
            sv_name = (sv.get("name") or "").strip()

            fv = self._find_match(unmapped, sv_vin, sv_plate, sv_name)
            if not fv:
                unmatched_samsara.append((sv_id, sv_name, sv_vin, sv_plate))
                continue

            unmapped.remove(fv)
            matched.append((sv_id, sv_name, fv, sv_vin, sv_plate))

            if apply_changes:
                fv.samsara_vehicle_id = sv_id
                if not fv.vin and sv_vin:
                    fv.vin = sv_vin
                if not fv.license_plate and sv_plate:
                    fv.license_plate = sv_plate
                fv.samsara_last_synced_at = timezone.now()
                fv.save(update_fields=[
                    "samsara_vehicle_id", "vin", "license_plate", "samsara_last_synced_at",
                ])

        # Report
        verb = "MAPPED" if apply_changes else "WOULD MAP"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(matched)} vehicle(s):"))
        for sv_id, sv_name, fv, vin, plate in matched:
            self.stdout.write(f"  {sv_id}  {sv_name!r:<22} -> #{fv.vehicle_number} ({fv})")

        if unmatched_samsara:
            self.stdout.write(self.style.WARNING(
                f"\n{len(unmatched_samsara)} samsara vehicle(s) had no FleetVehicle match:"
            ))
            for sv_id, sv_name, vin, plate in unmatched_samsara:
                self.stdout.write(f"  {sv_id}  {sv_name!r}  vin={vin!r}  plate={plate!r}")

        if unmapped:
            self.stdout.write(self.style.WARNING(
                f"\n{len(unmapped)} FleetVehicle(s) still unmapped after sync:"
            ))
            for fv in unmapped:
                self.stdout.write(f"  #{fv.vehicle_number}  {fv}")

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                "\n(dry run - re-run with --apply to write matches)"
            ))

    @staticmethod
    def _find_match(candidates, vin, plate, name):
        if vin:
            for fv in candidates:
                if fv.vin and fv.vin.strip().upper() == vin.upper():
                    return fv
        if plate:
            for fv in candidates:
                if fv.license_plate and fv.license_plate.strip().upper() == plate.upper():
                    return fv
        if name:
            for fv in candidates:
                if fv.vehicle_number and fv.vehicle_number.strip() == name:
                    return fv
        return None
