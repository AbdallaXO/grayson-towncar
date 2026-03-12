"""
Safety data migration: mark all historical past-due leads as LOST.

Production has only ~135 lost leads, but there are likely thousands of leads
with status new/contacted/interested/future_contact whose pickup date has
already passed and who never converted. These should be LOST for accurate data.

Uses bulk .update() (not per-row .save()) so it:
- Runs in seconds, not hours
- Does NOT trigger signals (no GHL spam for historical data)
- Does NOT create per-lead LeadActivity entries (too many, not useful for old data)

Going forward, the `detect_lost_leads` scheduler task handles this hourly.
"""

from django.db import migrations
from django.utils import timezone


def backfill_lost_leads(apps, schema_editor):
    Lead = apps.get_model("reservations", "Lead")
    today = timezone.now().date()

    # Mark all past-due, non-converted leads as lost
    updated = Lead.objects.filter(
        pickup_date__lt=today,
        status__in=["new", "contacted", "interested", "future_contact"],
        converted=False,
    ).update(status="lost")

    if updated:
        print(f"\n  Backfilled {updated} past-due leads to status=lost")


def reverse_backfill(apps, schema_editor):
    # No-op: we can't know which ones were genuinely active vs past-due
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0083_backfill_initial_sms_sent"),
    ]

    operations = [
        migrations.RunPython(backfill_lost_leads, reverse_backfill),
    ]
