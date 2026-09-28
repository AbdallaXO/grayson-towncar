"""No two logins may differ only by capitals.

Agent registration once checked usernames case-sensitively, so "JamieTodd" and
"jamietodd" could both exist — and a sign-in lookup that ignores case then
matched two accounts and 500'd. Every path that creates a login now checks
case-insensitively; this index is the backstop for any path added later.

The five existing pairs are folded by `manage.py merge_duplicate_agents
--apply`, which MUST run before this migration: an index cannot be built over
rows that already break it. The pre-check below says so in words instead of
leaving a bare IntegrityError in the deploy log.

LOWER(username) expression indexes work on both Postgres (production) and
SQLite (local), so one statement serves both.
"""
from django.db import migrations

INDEX = "auth_user_username_lower_uniq"


def refuse_if_duplicates(apps, schema_editor):
    User = apps.get_model("auth", "User")
    from django.db.models import Count
    from django.db.models.functions import Lower

    dupes = list(User.objects.annotate(u=Lower("username")).values("u")
                 .annotate(n=Count("id")).filter(n__gt=1).values_list("u", flat=True)[:10])
    if dupes:
        raise RuntimeError(
            "Cannot add the case-insensitive username index: these usernames exist "
            f"more than once ignoring capitals: {dupes}. Run "
            "`python manage.py merge_duplicate_agents --apply` first.")


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0035_userprofile_shift_end_userprofile_shift_start"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(refuse_if_duplicates, migrations.RunPython.noop),
        migrations.RunSQL(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON auth_user (LOWER(username));",
            f"DROP INDEX IF EXISTS {INDEX};",
        ),
    ]
