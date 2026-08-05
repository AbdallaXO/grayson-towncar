"""
Run the Fleet Management nightly reconcile by hand.

    python manage.py fleet_reconcile              # full pass, respects nothing
    python manage.py fleet_reconcile --dry-run    # report state, write nothing
    python manage.py fleet_reconcile --accrue-only

The background poller calls the same functions on a local-clock gate. This
command exists so the pass can be run and inspected on demand — run it manually
for a week before trusting the scheduled path, and reach for it whenever the
feed-health tile says the nightly has not landed.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from dispatching.fleet_sync import (
    FEED_NIGHTLY,
    accrue_vehicle_day,
    finalise_previous_day,
    reconcile_fleet,
    refresh_vehicle_master,
    should_reconcile,
)
from dispatching.samsara_service import SamsaraService


class Command(BaseCommand):
    help = "Run (or inspect) the Fleet Management reconcile."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what the gate and the data look like; write nothing.",
        )
        parser.add_argument(
            "--accrue-only", action="store_true",
            help="Only upsert today's mileage rows; skip master refresh.",
        )

    def handle(self, *args, **options):
        from drivers.models import FleetSyncState, FleetVehicle, VehicleDayReading

        now = timezone.now()
        local = timezone.localtime(now)
        service = SamsaraService()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nFleet reconcile — local time {local:%Y-%m-%d %H:%M} "
            f"({timezone.get_current_timezone_name()})\n"
        ))

        if not service.is_configured():
            self.stdout.write(self.style.ERROR(
                "SAMSARA_API_TOKEN is not set — the master refresh will no-op.\n"
                "Mileage accrual still runs off whatever telemetry is stored.\n"
            ))

        if options["dry_run"]:
            self._report(now, FleetSyncState, FleetVehicle, VehicleDayReading)
            self.stdout.write(self.style.NOTICE("\n(dry run — nothing written)\n"))
            return

        if options["accrue_only"]:
            out = accrue_vehicle_day(now=now)
            self.stdout.write(self.style.SUCCESS(
                f"Accrual: {out.get('rows', 0)} row(s) "
                f"({out.get('created', 0)} new) — {out.get('status')}"
            ))
            return

        summary = reconcile_fleet(now=now, service=service)
        style = self.style.SUCCESS if summary["status"] == "success" else self.style.WARNING
        self.stdout.write(style(f"\nReconcile: {summary['status']}"))
        for step, out in summary["steps"].items():
            detail = ", ".join(f"{k}={v}" for k, v in out.items() if k != "status")
            self.stdout.write(f"  {step:<10} {out.get('status'):<10} {detail}")

        master = summary["steps"].get("master", {})
        if master.get("vin_drift"):
            self.stdout.write(self.style.ERROR(
                f"\n  {master['vin_drift']} VIN DRIFT event(s) — a gateway may have "
                f"moved between cars. Check the AuditLog; mileage history for the "
                f"affected vehicles is suspect until resolved."
            ))
        self.stdout.write("")

    def _report(self, now, FleetSyncState, FleetVehicle, VehicleDayReading):
        today = timezone.localdate(now)

        gate = should_reconcile(now=now)
        self.stdout.write(f"  nightly gate open right now: {gate}")
        state = FleetSyncState.objects.filter(feed=FEED_NIGHTLY).first()
        if state:
            self.stdout.write(
                f"  last nightly success:        {state.last_success_at or 'never'} "
                f"(status={state.last_status or '-'}, "
                f"consecutive failures={state.consecutive_failures})"
            )
        else:
            self.stdout.write("  last nightly success:        never run")

        mapped = FleetVehicle.objects.exclude(samsara_vehicle_id="").count()
        total = FleetVehicle.objects.count()
        with_odo = FleetVehicle.objects.filter(
            samsara_odometer_meters__isnull=False
        ).count()
        self.stdout.write(
            f"\n  vehicles: {total} total, {mapped} mapped to Samsara, "
            f"{with_odo} with a stored odometer"
        )

        rows_today = VehicleDayReading.objects.filter(date=today).count()
        known = VehicleDayReading.objects.filter(
            date=today, miles_driven__isnull=False
        ).count()
        self.stdout.write(
            f"  today's rows: {rows_today} ({known} with a known mileage; "
            f"the rest render as an em-dash, NOT zero)"
        )

        total_rows = VehicleDayReading.objects.count()
        self.stdout.write(f"  day rows all time: {total_rows}")
        if total_rows == 0:
            self.stdout.write(self.style.WARNING(
                "  (expected on day one — this table accrues forward. Samsara's "
                "/fleet/vehicles/stats/history endpoint is entitled, so a past "
                "window can be backfilled later; nothing in our own DB can.)"
            ))
