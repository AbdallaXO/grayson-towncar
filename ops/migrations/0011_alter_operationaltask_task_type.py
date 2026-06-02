# Generated for the early-flight tight-turn safety net.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ops", "0010_add_staff_scheduling"),
    ]

    operations = [
        migrations.AlterField(
            model_name="operationaltask",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("payment_chase", "Unpaid Reservations"),
                    ("flight_verify", "Flight Verification"),
                    ("driver_conflict", "Driver Conflict"),
                    ("driver_assign", "Driver Assignment"),
                    ("confirmation_texts", "Confirmation Texts"),
                    ("contact_form", "Contact Us"),
                    ("afterhours_fee", "After-Hours Fee"),
                    ("tight_turn", "Tight Turn"),
                    ("manual", "Manual Task"),
                ],
                db_index=True,
                max_length=30,
            ),
        ),
    ]
