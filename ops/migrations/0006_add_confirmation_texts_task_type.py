# Adds the confirmation_texts task type for the daily next-day SMS batch task.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ops', '0005_add_emaillog_model'),
    ]

    operations = [
        migrations.AlterField(
            model_name='operationaltask',
            name='task_type',
            field=models.CharField(
                choices=[
                    ('payment_chase', 'Unpaid Reservations'),
                    ('flight_verify', 'Flight Verification'),
                    ('driver_conflict', 'Driver Conflict'),
                    ('driver_assign', 'Driver Assignment'),
                    ('confirmation_texts', 'Confirmation Texts'),
                    ('contact_form', 'Contact Us'),
                    ('manual', 'Manual Task'),
                ],
                db_index=True,
                max_length=30,
            ),
        ),
    ]
