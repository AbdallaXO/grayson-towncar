"""
Bulk auto-conversion sweep — the offline, crash-proof equivalent of the admin
"Check for Auto-Conversion" action.

The admin action now uses the same scale-safe engine
(`reservations.lead_matching.recheck_lead_conversions`), but for a very large
historical sweep (thousands of leads) running it offline via manage.py removes
all risk of the web worker's 60s timeout and lets you preview with --dry-run.

Matches by confidence (gclid > email+phone > email > phone) and FLAGS — does not
merge — a phone-only match to a different-looking person. Run --dry-run first.
"""
from django.core.management.base import BaseCommand

from reservations.lead_matching import ACTIVE_STATUSES, recheck_lead_conversions
from reservations.models import Lead


class Command(BaseCommand):
    help = "Auto-convert leads that match an existing reservation (gclid/email/phone confidence)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would convert/flag without writing.",
        )
        parser.add_argument(
            "--all-statuses", action="store_true",
            help="Consider leads in ANY non-converted status (default: only "
                 f"active statuses {ACTIVE_STATUSES}).",
        )
        parser.add_argument(
            "--no-ghl-sync", action="store_true",
            help="Skip pushing the 'converted' status to GoHighLevel afterward.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        qs = Lead.objects.exclude(converted=True)
        if not options["all_statuses"]:
            qs = qs.filter(status__in=ACTIVE_STATUSES)

        report = recheck_lead_conversions(qs, dry_run=dry_run)

        self.stdout.write(report.summary())

        # The engine uses bulk_update (skips signals) for speed, so GHL is not
        # auto-synced. Do it here serially — safe offline (no web-worker timeout),
        # and the admin action does the equivalent in the background.
        if not dry_run and not options["no_ghl_sync"] and report.converted_lead_ids:
            self._sync_converted_to_ghl(report.converted_lead_ids)

        if dry_run and report.converted_lead_ids:
            self.stdout.write(
                f"  Would convert lead ids: {report.converted_lead_ids[:50]}"
                + (" ..." if len(report.converted_lead_ids) > 50 else "")
            )
        self.stdout.write(self.style.SUCCESS("Done."))

    def _sync_converted_to_ghl(self, lead_ids):
        """Push 'converted' to GHL for the just-converted leads that have a GHL
        contact, serially. Best-effort: a failure is logged, never fatal."""
        import time

        from ghl_integration.services import GoHighLevelService

        pairs = list(
            Lead.objects.filter(id__in=lead_ids, ghl_contact_id__isnull=False)
            .values_list("id", "ghl_contact_id")
        )
        if not pairs:
            return
        service = GoHighLevelService()
        synced = 0
        for lead_id, contact_id in pairs:
            try:
                if service.update_contact_status_fields(contact_id=contact_id, status="converted"):
                    synced += 1
            except Exception as e:
                self.stderr.write(f"  GHL sync failed for Lead #{lead_id}: {e}")
            time.sleep(0.2)  # gentle rate-limit
        self.stdout.write(f"  Synced 'converted' to GHL for {synced}/{len(pairs)} leads.")
