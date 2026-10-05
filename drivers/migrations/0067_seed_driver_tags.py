"""Seed the driver-knowledge tags: strengths, habits, languages and areas.

The list is the Stage 1 plan's (docs/scheduling-redesign/
08_STAGE1_FOUNDATION_PLAN.md, Task 9, S19). sort_order runs through each
category in the plan's order, so a category's positive tags come before its
caution ones. Managers add more from the driver profile.

Forwards never touches a tag that is already there under any capitalisation,
so re-running it is a no-op. Backwards removes the seeded tags no driver has;
one in use stays, because the link is PROTECT and deleting it would wipe what
the desk wrote about that driver.
"""
from django.db import migrations


SEED = [
    ("strength", "positive", ["Airport pro", "Cruise port pro", "VIP & corporate",
                              "Large groups", "Car seats & families", "Long-distance trips",
                              "Calm under pressure", "Great guest reviews"]),
    ("habit", "positive", ["Always early", "Taps every status", "Picks up extra shifts",
                           "Keeps the car spotless"]),
    ("habit", "caution", ["Runs late", "Slow with luggage", "Misses status taps",
                          "Hard to reach by phone", "Prefers no late nights"]),
    ("language", "positive", ["Spanish", "Portuguese", "French", "Haitian Creole", "Arabic"]),
    ("area", "positive", ["Disney", "Universal", "Port Canaveral", "Downtown Orlando", "Tampa"]),
]


def _rows():
    """(category, polarity, name, sort_order), numbered 1.. within each category."""
    position = {}
    for category, polarity, names in SEED:
        for name in names:
            position[category] = position.get(category, 0) + 1
            yield category, polarity, name, position[category]


def seed(apps, schema_editor):
    DriverTag = apps.get_model("drivers", "DriverTag")
    for category, polarity, name, sort_order in _rows():
        if not DriverTag.objects.filter(name__iexact=name).exists():
            DriverTag.objects.create(name=name, category=category, polarity=polarity,
                                     sort_order=sort_order)


def unseed(apps, schema_editor):
    DriverTag = apps.get_model("drivers", "DriverTag")
    names = [name for _, _, name, _ in _rows()]
    DriverTag.objects.filter(name__in=names, assignments__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("drivers", "0066_driver_tags"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
