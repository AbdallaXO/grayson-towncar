"""
Management command to backfill follow-up sequences for existing leads.

Finds leads that already received the initial SMS (step 1) but don't have
a follow-up sequence yet, and creates steps 2-5 for them.

Usage:
    python manage.py backfill_followup_sequences [--dry-run] [--limit N]
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from ghl_integration.models import FollowUpTask, LeadActivity
from ghl_integration.segmentation import classify_lead
from ghl_integration.tasks import STEP_DELAYS
from ghl_integration.timing import adjust_to_send_window
from reservations.models import Lead


class Command(BaseCommand):
    help = "Backfill follow-up sequences for existing leads that already received initial SMS"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of leads to process (default: 100)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        # Find leads that:
        # - Have received initial SMS (step 1)
        # - Have NOT replied
        # - Are NOT converted
        # - Don't have an active sequence
        # - Pickup date is in the future
        eligible_leads = Lead.objects.filter(
            initial_sms_sent=True,
            has_replied=False,
            converted=False,
            sequence_active=False,
            pickup_date__gte=timezone.now().date(),
        ).exclude(
            status__in=[Lead.StatusChoices.LOST, Lead.StatusChoices.COLD, Lead.StatusChoices.CONVERTED],
        ).exclude(
            follow_up_tasks__step_number=2,  # Already has step 2 = already backfilled
        ).order_by("-created_at")[:limit]

        total = eligible_leads.count()
        self.stdout.write(f"Found {total} eligible leads for backfill")

        if dry_run:
            for lead in eligible_leads:
                segment = classify_lead(lead)
                self.stdout.write(
                    f"  [DRY RUN] Lead #{lead.id} {lead.first_name} {lead.last_name} "
                    f"(segment={segment}, sms_sent_at={lead.initial_sms_sent_at})"
                )
            self.stdout.write(self.style.WARNING(f"Dry run complete. {total} leads would be processed."))
            return

        processed = 0
        for lead in eligible_leads:
            segment = classify_lead(lead)
            lead.segment = segment
            lead.sequence_active = True
            lead.save(update_fields=["segment", "sequence_active"])

            base_time = lead.initial_sms_sent_at or lead.created_at

            # Create step 1 record (already sent)
            FollowUpTask.objects.update_or_create(
                lead=lead,
                step_number=1,
                defaults={
                    "segment": segment,
                    "status": FollowUpTask.StatusChoices.SENT,
                    "scheduled_at": base_time,
                    "sent_at": base_time,
                    "message_body": "(initial SMS — backfilled)",
                },
            )

            # Create steps 2-5
            for step in range(2, 6):
                scheduled_raw = base_time + timedelta(hours=STEP_DELAYS[step])
                scheduled_at = adjust_to_send_window(scheduled_raw)

                # If scheduled time is in the past, skip (don't send overdue messages)
                if scheduled_at < timezone.now():
                    FollowUpTask.objects.update_or_create(
                        lead=lead,
                        step_number=step,
                        defaults={
                            "segment": segment,
                            "status": FollowUpTask.StatusChoices.SKIPPED,
                            "scheduled_at": scheduled_at,
                        },
                    )
                else:
                    FollowUpTask.objects.update_or_create(
                        lead=lead,
                        step_number=step,
                        defaults={
                            "segment": segment,
                            "status": FollowUpTask.StatusChoices.PENDING,
                            "scheduled_at": scheduled_at,
                        },
                    )

            LeadActivity.objects.create(
                lead=lead,
                activity_type=LeadActivity.ActivityType.SEQUENCE_STARTED,
                description=f"Follow-up sequence backfilled (segment={segment}). Steps 2-5 created.",
                metadata={"segment": segment, "backfill": True},
            )

            processed += 1
            self.stdout.write(f"  Backfilled Lead #{lead.id} ({lead.first_name} {lead.last_name}, segment={segment})")

        self.stdout.write(self.style.SUCCESS(f"Done. Backfilled {processed} leads."))
