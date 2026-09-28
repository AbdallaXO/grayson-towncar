"""Close the "Possible duplicate reservation" tasks the reminder engine filed.

Founder decision 2026-09-22: those tasks were never worked — the Duplicate
Reservations page is where a duplicate gets resolved, and it now sorts every
unpaid twin into safe / check first / hold on its own. The engine no longer
files them (ops/unpaid_reminders.py); this closes the ones already sitting in
Ops Control, with a note that says where the work went. The reservations keep
their "suspected duplicate" flag, which the engine now clears by itself once
the paid twin is gone.
"""
from django.db import migrations
from django.db.models import Q
from django.utils import timezone

OPEN = ("pending", "in_progress", "snoozed", "escalated")
NOTE = (
    "Retired: duplicates are resolved on the Duplicate Reservations page, "
    "which now sorts them into safe / check first / hold."
)


def close_duplicate_tasks(apps, schema_editor):
    OperationalTask = apps.get_model("ops", "OperationalTask")
    qs = OperationalTask.objects.filter(
        task_type="payment_chase", status__in=OPEN
    ).filter(
        Q(metadata__trigger="duplicate_suspected")
        | Q(title__startswith="Possible duplicate reservation")
    )
    qs.update(status="cancelled", resolved_at=timezone.now(), resolution_notes=NOTE)


class Migration(migrations.Migration):
    dependencies = [
        ("ops", "0024_close_tight_turn_tasks"),
    ]

    operations = [
        migrations.RunPython(close_duplicate_tasks, migrations.RunPython.noop),
    ]
