# Schema-noop: adds the 'conflict_advisor' trigger choice to ScheduleSnapshot
# (Auto-save Before Advisor Apply — the Recovery Advisor's one-click apply path
# snapshots the day before multi-move / farm / retime plans touch the board).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0123_routedistancecache"),
    ]

    operations = [
        migrations.AlterField(
            model_name="schedulesnapshot",
            name="trigger",
            field=models.CharField(
                choices=[
                    ("manual", "Manual Save"),
                    ("before_reset", "Auto-save Before Reset"),
                    ("before_auto_assign", "Auto-save Before Auto-Assign"),
                    ("conflict_advisor", "Auto-save Before Advisor Apply"),
                ],
                default="manual",
                max_length=30,
            ),
        ),
    ]
