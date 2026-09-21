"""Close the QUOTE NEEDED tasks still open when the task type was retired.

Founder decision 2026-09-21: quote requests are answered in GoHighLevel, where
the guest's reply lands and where the engine's suggested price now sits on the
contact card. New requests no longer file a task (reservations/views.py), and
the ones already open would otherwise sit in Ops Control until someone closed
them by hand. This closes them once, with a note that says where the work went.
"""
from django.db import migrations
from django.utils import timezone

OPEN = ("pending", "in_progress", "snoozed", "escalated")
NOTE = "Retired: quote requests are answered in GoHighLevel now — the suggested price is on the contact card."


def close_quote_tasks(apps, schema_editor):
    OperationalTask = apps.get_model("ops", "OperationalTask")
    qs = OperationalTask.objects.filter(task_type="manual", status__in=OPEN).filter(
        title__startswith="QUOTE NEEDED"
    )
    qs.update(status="cancelled", resolved_at=timezone.now(), resolution_notes=NOTE)


class Migration(migrations.Migration):
    dependencies = [
        ("ops", "0022_afterhours_settled_activity"),
    ]

    operations = [
        migrations.RunPython(close_quote_tasks, migrations.RunPython.noop),
    ]
