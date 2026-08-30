"""Keep the two founder accounts and the placeholder record out of payroll.

The Driver Payments page filters on nothing but "has unpaid legs", so these
three have been showing up in every payroll run with legs going back to July
2025 that nobody intends to pay. Marking `is_active = False` never helped —
that flag gates the directory and assignment pickers, not payroll.

BY ID ONLY, never by name. `Raymond` (id 64, Ray Rivera) and `Abderrahmane`
(id 24) both match a founder name pattern and are real drivers who are really
paid; excluding either would silently stop their money.

  1 - Rayyan Vorajee (co-founder, superuser)
  6 - "placeholder" (no name, no email, dead since May 2025, never had a
      statement in its life)
  9 - Abdalla (founder, superuser)

Idempotent: re-running sets the same three rows to the same value.
"""
from django.db import migrations

FOUNDER_DRIVER_IDS = [1, 6, 9]


def exclude_founders(apps, schema_editor):
    Driver = apps.get_model("drivers", "Driver")
    Driver.objects.filter(id__in=FOUNDER_DRIVER_IDS).update(exclude_from_payroll=True)


def include_founders(apps, schema_editor):
    Driver = apps.get_model("drivers", "Driver")
    Driver.objects.filter(id__in=FOUNDER_DRIVER_IDS).update(exclude_from_payroll=False)


class Migration(migrations.Migration):

    dependencies = [
        ("drivers", "0050_driver_exclude_from_payroll"),
    ]

    operations = [
        migrations.RunPython(exclude_founders, include_founders),
    ]
