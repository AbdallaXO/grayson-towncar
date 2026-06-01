from django.db import migrations


# Pre-pickup nudge templates (step 6). One row per offer "variant". These are
# the only two variants that auto-send an SMS: the discount variant routes the
# lead to a human (no automated price), and the free-upgrade variant is out of
# scope for v1. Editable via Django admin afterward — no redeploy needed.
#
# Placeholders: {first_name}, {pickup_date}, {pickup_location}, {dropoff_location},
# {estimated_price}. (The renderer also supports {booking_link}, but the default
# copy is reply-driven — no raw link — matching the existing 5-step sequence.)
TEMPLATES = [
    {
        "step_number": 6,
        "segment": "pre_pickup_urgency",
        "delay_hours": 0,  # date-anchored (pickup-3d); delay_hours unused for step 6
        "message_template": (
            "Hi {first_name}! Your trip on {pickup_date} is coming up \U0001F5D3 "
            "Just making sure you've got a ride locked in from {pickup_location} to "
            "{dropoff_location}. We're filling up for that day — want me to confirm "
            "your {estimated_price} flat rate before we're booked? Just reply YES "
            "and I'll lock it in."
        ),
    },
    {
        "step_number": 6,
        "segment": "pre_pickup_cruise_urgency",
        "delay_hours": 0,
        "message_template": (
            "Hi {first_name}! Your sailing on {pickup_date} is almost here. "
            "Don't leave your port transfer to chance — we'll have a driver confirmed "
            "and waiting so you board stress-free. Want me to lock in your "
            "{estimated_price} transfer? Just reply YES."
        ),
    },
]


def seed_templates(apps, schema_editor):
    FollowUpSequence = apps.get_model("ghl_integration", "FollowUpSequence")
    for t in TEMPLATES:
        FollowUpSequence.objects.update_or_create(
            step_number=t["step_number"],
            segment=t["segment"],
            defaults={
                "delay_hours": t["delay_hours"],
                "message_template": t["message_template"],
                "is_active": True,
            },
        )


def remove_templates(apps, schema_editor):
    FollowUpSequence = apps.get_model("ghl_integration", "FollowUpSequence")
    FollowUpSequence.objects.filter(step_number=6).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ghl_integration", "0004_alter_followupsequence_delay_hours_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
