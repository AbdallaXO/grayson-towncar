"""Seed the fourth regular-shift shape: Float, "any shift" (S16).

A Float driver goes wherever the day needs him. The bands span the other three
shapes (leaves base 3 AM–4 PM, back noon–2:15 AM), and a Float day is still
capped at 12 hours base to base and clipped by his own limits. See
docs/scheduling-redesign/08_STAGE1_FOUNDATION_PLAN.md, S16 and S18.

Forwards never overwrites a Float row that is already there. Backwards removes
Float only while nothing uses it — no weekly row (as the day's shift or its
second shift) and no driver's usual shift. Once a manager has picked it, it is
left in place, as drivers 0063 leaves the other three: the links are PROTECT,
so deleting it would either fail or wipe managers' regular shifts.
"""
from datetime import time

from django.db import migrations
from django.db.models import Q


FLOAT = {
    "name": "Float", "sort_order": 4,
    "start_earliest": time(3, 0), "start_latest": time(16, 0),
    "end_earliest": time(12, 0), "end_latest": time(2, 15),
    "max_span_minutes": 720,
    "notes": "Any shape — goes wherever the day needs him, still within 12 hours and "
             "his limits.",
}


def seed(apps, schema_editor):
    ShiftTemplate = apps.get_model("drivers", "ShiftTemplate")
    ShiftTemplate.objects.get_or_create(kind="float", defaults=FLOAT)


def unseed(apps, schema_editor):
    ShiftTemplate = apps.get_model("drivers", "ShiftTemplate")
    Driver = apps.get_model("drivers", "Driver")
    DriverWeeklySchedule = apps.get_model("drivers", "DriverWeeklySchedule")
    floater = ShiftTemplate.objects.filter(kind="float").first()
    if floater is None:
        return
    in_use = (DriverWeeklySchedule.objects
              .filter(Q(shift_template=floater) | Q(alt_template=floater)).exists()
              or Driver.objects.filter(shift_role=floater).exists())
    if not in_use:
        floater.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("drivers", "0064_usual_shift_and_day_options"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
