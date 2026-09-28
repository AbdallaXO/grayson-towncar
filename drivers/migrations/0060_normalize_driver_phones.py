"""Store every driver phone number the same way (E.164).

Rewrites only values drivers/phones.py can parse — a 10-digit US number in any
punctuation, or 11 digits starting with 1. Anything else (short, garbled, or
already international) is left exactly as it was, so no number on file is
lost. Reversible as a no-op: the old free-text forms carried no information the
E.164 form does not.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from drivers import phones

    Driver = apps.get_model("drivers", "Driver")
    for driver in Driver.objects.exclude(phone_number__isnull=True).exclude(phone_number=""):
        normalized = phones.normalize(driver.phone_number)
        if normalized and normalized != driver.phone_number:
            Driver.objects.filter(pk=driver.pk).update(phone_number=normalized)


class Migration(migrations.Migration):
    dependencies = [
        ("drivers", "0059_driver_onboarding_and_invites"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
