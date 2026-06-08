"""
Manual / debug entry point for the Samsara live-position sync.

    python manage.py samsara_sync_vehicles
        Run one poll cycle now (same work the background poller does every 3 min).

    python manage.py samsara_sync_vehicles --list-mappings
        Print FleetVehicles split into mapped vs un-mapped, alongside the live
        Samsara vehicle list — so you can wire each car's samsara_vehicle_id in
        the Django admin incrementally as the fleet onboards.
"""
from django.core.management.base import BaseCommand

from drivers.models import FleetVehicle
from dispatching.samsara_service import SamsaraService
from dispatching.samsara_scheduler import sync_vehicles, sweep_eta


class Command(BaseCommand):
    help = "Sync Samsara live vehicle positions, or list FleetVehicle <-> Samsara mappings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list-mappings",
            action="store_true",
            help="Show mapped/un-mapped FleetVehicles next to the live Samsara vehicle list.",
        )

    def handle(self, *args, **options):
        service = SamsaraService()
        if not service.is_configured():
            self.stdout.write(self.style.WARNING(
                "SAMSARA_API_TOKEN is not set — Samsara integration is inert. "
                "Add the token to .env / Railway to enable it."
            ))
            return

        if options["list_mappings"]:
            self._list_mappings(service)
            return

        summary = sync_vehicles()
        status = summary.get("status")
        if status == "success":
            self.stdout.write(self.style.SUCCESS(
                f"Synced {summary.get('updated', 0)} vehicle(s)."
            ))
        elif status == "skipped":
            self.stdout.write(self.style.WARNING(
                f"Skipped: {summary.get('reason')}."
            ))
        else:
            self.stdout.write(self.style.ERROR(
                f"Sync did not succeed: {status} ({summary.get('error')})."
            ))

        # Compute the schedule-aware ETA / late-risk badges for today's legs.
        eta = sweep_eta()
        self.stdout.write(self.style.SUCCESS(
            f"ETA sweep: flagged {eta.get('flagged', 0)} leg(s) across "
            f"{eta.get('drivers', 0)} driver(s)."
        ))

    def _list_mappings(self, service):
        mapped = FleetVehicle.objects.exclude(samsara_vehicle_id="").order_by("vehicle_number")
        unmapped = FleetVehicle.objects.filter(samsara_vehicle_id="").order_by("vehicle_number")

        self.stdout.write(self.style.MIGRATE_HEADING("\nMapped FleetVehicles:"))
        if mapped:
            for v in mapped:
                self.stdout.write(f"  {v.vehicle_number:<10} -> samsara_vehicle_id={v.samsara_vehicle_id}")
        else:
            self.stdout.write("  (none)")

        self.stdout.write(self.style.MIGRATE_HEADING("\nUn-mapped FleetVehicles (set samsara_vehicle_id in admin):"))
        if unmapped:
            for v in unmapped:
                self.stdout.write(f"  {v.vehicle_number:<10}  {v.year} {v.make} {v.model}")
        else:
            self.stdout.write("  (none)")

        self.stdout.write(self.style.MIGRATE_HEADING("\nVehicles available in Samsara:"))
        result = service.list_vehicles()
        if result.get("status") != "success":
            self.stdout.write(self.style.ERROR(
                f"  Could not list Samsara vehicles: {result.get('status')} ({result.get('error')})"
            ))
            self.stdout.write(
                "  (If this is an auth/entitlement error, confirm the token and that "
                "Vehicle Stats / GPS is enabled on the Samsara plan.)"
            )
            return
        vehicles = result.get("data", [])
        if not vehicles:
            self.stdout.write("  (none returned)")
            return
        for sv in vehicles:
            sid = sv.get("id", "")
            name = sv.get("name", "")
            plate = sv.get("licensePlate", "")
            vin = sv.get("vin", "")
            extra = " ".join(p for p in [f"plate={plate}" if plate else "", f"vin={vin}" if vin else ""] if p)
            self.stdout.write(f"  id={sid:<20} name={name:<16} {extra}")
