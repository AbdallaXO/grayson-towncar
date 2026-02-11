from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0071_add_notes_to_schedulesnapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='leg',
            name='exclude_from_analytics',
            field=models.BooleanField(default=False, help_text='Exclude this leg from route timing analytics (bad data)'),
        ),
    ]
