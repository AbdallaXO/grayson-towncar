"""
Safety data migration: mark all existing non-new leads as initial_sms_sent=True.

This prevents the follow-up engine from re-sending SMS to the ~11,700 leads
that were already contacted by the old system. Without this, deploying the
new scheduler would cause it to pick up all leads with initial_sms_sent=False
and status IN ('new','contacted') — triggering mass SMS resends.

Only leads with status='new' are left untouched so they flow through normally.
"""

from django.db import migrations


def backfill_initial_sms_sent(apps, schema_editor):
    Lead = apps.get_model("reservations", "Lead")
    # Mark all non-new leads as already sent
    updated = Lead.objects.exclude(status="new").filter(
        initial_sms_sent=False
    ).update(initial_sms_sent=True)
    if updated:
        print(f"\n  Backfilled initial_sms_sent=True for {updated} existing leads")


def reverse_backfill(apps, schema_editor):
    # No-op: we can't know which ones were genuinely unsent
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0082_lead_followup_fields"),
    ]

    operations = [
        migrations.RunPython(backfill_initial_sms_sent, reverse_backfill),
    ]
