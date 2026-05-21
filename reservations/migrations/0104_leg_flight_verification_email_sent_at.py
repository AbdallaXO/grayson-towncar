from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0103_add_unpaid_reminder_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='leg',
            name='flight_verification_email_sent_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "When the self-service flight verification email was last sent "
                    "to the guest. Cleared when the guest acts on the link so a fresh "
                    "verification cycle can start if needed."
                ),
            ),
        ),
        migrations.AddField(
            model_name='historicalleg',
            name='flight_verification_email_sent_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "When the self-service flight verification email was last sent "
                    "to the guest. Cleared when the guest acts on the link so a fresh "
                    "verification cycle can start if needed."
                ),
            ),
        ),
    ]
