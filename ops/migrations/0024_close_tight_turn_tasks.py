"""Close the Tight Turn tasks still open when the type was retired.

Founder decision 2026-09-22: a driver who reaches the airport inside the meet
deadline (gate + 10) is on time, so the amber "tight turn" task described a
non-problem — ~73 a day, a third of them 1–3 minutes "late", none of which
anyone could act on. The scanner no longer files them (ops/tasks.py); the
board's driver timeline still draws the tight gap live. This closes the rows
already in Ops Control once, with a note that says why. A turn the driver
genuinely can't make still files as a Driver Conflict, unchanged.
"""
from django.db import migrations
from django.utils import timezone

OPEN = ("pending", "in_progress", "snoozed", "escalated")
# Same wording as ops.tasks.TIGHT_TURN_RETIRED_NOTE (not imported: migrations
# must not depend on live module code).
NOTE = (
    "Auto-closed: tight turns no longer file as tasks — the driver makes the "
    "meet deadline. A turn he can't make still files as a Driver Conflict."
)


def close_tight_turn_tasks(apps, schema_editor):
    OperationalTask = apps.get_model("ops", "OperationalTask")
    OperationalTask.objects.filter(task_type="tight_turn", status__in=OPEN).update(
        status="completed", resolved_at=timezone.now(), resolution_notes=NOTE
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ops", "0023_close_quote_needed_tasks"),
    ]

    operations = [
        migrations.RunPython(close_tight_turn_tasks, migrations.RunPython.noop),
    ]
