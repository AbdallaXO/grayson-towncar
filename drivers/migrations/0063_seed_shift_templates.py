"""Seed the three regular-shift shapes: Morning, Midday and Evening.

Bands come from §3 of docs/scheduling-redesign/07_STRUCTURED_SHIFTS_DESIGN.md
(8,787 trips over 66 days). Two values are choices made in the Stage 1 plan
(08_STAGE1_FOUNDATION_PLAN.md, S8), not measurements: the lower end of
Midday's end band (3 PM) and of Evening's end band (8 PM). Every shape is
capped at 12 hours base to base. The bands are targets a manager can tune on
the Shift Templates page; a regular shift outside them only gets a warning.

Forwards never overwrites a shape that is already there. Backwards removes the
three seeded shapes (it refuses while a weekly row still points at one).
"""
from datetime import time

from django.db import migrations


SEED = [
    {
        "kind": "morning", "name": "Morning", "sort_order": 1,
        "start_earliest": time(3, 0), "start_latest": time(6, 0),
        "end_earliest": time(12, 0), "end_latest": time(16, 0),
        "notes": "Leaves base 3-6 AM. A 3 AM start is real for only 1-4 cars; most "
                 "leave 4-6 AM. Done somewhere between noon and 4 PM.",
    },
    {
        "kind": "midday", "name": "Midday", "sort_order": 2,
        "start_earliest": time(6, 0), "start_latest": time(9, 0),
        "end_earliest": time(15, 0), "end_latest": time(21, 0),
        "notes": "One driver, leaves base about 6-9 AM and is back no more than "
                 "12 hours later.",
    },
    {
        "kind": "evening", "name": "Evening", "sort_order": 3,
        "start_earliest": time(12, 0), "start_latest": time(16, 0),
        "end_earliest": time(20, 0), "end_latest": time(2, 15),
        "notes": "Starts at the handover: noon-2:30 PM on a crunch day, 2-4 PM on a "
                 "typical weekend, 1:30-3 PM on weekdays. The last car is back about "
                 "2:15 AM, after the day's last pickup (a median 11:32 PM MCO arrival).",
    },
]


def seed(apps, schema_editor):
    ShiftTemplate = apps.get_model("drivers", "ShiftTemplate")
    for row in SEED:
        values = {k: v for k, v in row.items() if k != "kind"}
        ShiftTemplate.objects.get_or_create(kind=row["kind"],
                                            defaults={**values, "max_span_minutes": 720})


def unseed(apps, schema_editor):
    ShiftTemplate = apps.get_model("drivers", "ShiftTemplate")
    ShiftTemplate.objects.filter(kind__in=[row["kind"] for row in SEED]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("drivers", "0062_shift_facts"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
