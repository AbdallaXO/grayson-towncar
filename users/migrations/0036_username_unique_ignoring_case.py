"""No two logins may differ only by capitals — and fold the pairs that already do.

Agent registration once checked usernames case-sensitively, so "JamieTodd" and
"jamietodd" could both exist, and a sign-in lookup that ignored case matched
both and 500'd. Every path that creates a login now checks case-insensitively;
the unique index below is the backstop for any path added later.

The index cannot be built over rows that already break it, so this migration
first merges the five known pairs (production data, reviewed 2026-09-28). It
runs here, on Railway's own migrate step, rather than from a laptop: the
settings deliberately have no path that connects a local machine to the
production database.

Per pair (keep, retire), each verified by id AND exact username — a pair that
does not match exactly is left alone, and the duplicate check after it then
stops the deploy in words:
  * reservations and commission payouts on the retired agent move to the kept
    one (the only two FKs to TravelAgent);
  * blank profile fields on the kept agent are filled from the retired one;
  * total_paid_commission is recomputed from the payouts; the cached unpaid /
    pending amounts travel with the rows (the app recalculates both whenever
    the agent opens their commission page, or via the admin action);
  * the retired login is switched off and renamed "<old>.merged-<keep id>",
    and its agent profile marked inactive. Nothing is deleted.

Historical models and QuerySet.update() only: no signal fires, so no welcome
or booking email goes anywhere. On a fresh database (tests, a new install) the
ids do not exist and every pair is a no-op.

LOWER(username) expression indexes work on Postgres (production) and SQLite
(local), so one statement serves both.
"""
from decimal import Decimal

from django.db import migrations
from django.db.models import Count, Sum
from django.db.models.functions import Lower

INDEX = "auth_user_username_lower_uniq"

# (keep user id, keep username, retire user id, retire username)
PAIRS = [
    (974, "gennyslack@mousecounselors.com", 940, "Gennyslack@mousecounselors.com"),
    (348, "hannahwarren", 126, "HannahWarren"),
    (1314, "jamietodd", 124, "JamieTodd"),
    (1439, "LindsaySchroeder", 1438, "lindsayschroeder"),
    (789, "Sunshinetravels17@yahoo.com", 294, "sunshinetravels17@yahoo.com"),
]
FILL_BLANKS = ("agent_name", "agency_name", "phone", "payment_method", "payment_info")


def merge_pairs(apps, schema_editor, pairs=None, log=print):
    User = apps.get_model("auth", "User")
    TravelAgent = apps.get_model("users", "TravelAgent")
    CommissionPayout = apps.get_model("users", "CommissionPayout")
    Reservation = apps.get_model("reservations", "Reservation")

    for keep_id, keep_name, retire_id, retire_name in (pairs if pairs is not None else PAIRS):
        keep = User.objects.filter(pk=keep_id, username=keep_name).first()
        retire = User.objects.filter(pk=retire_id, username=retire_name).first()
        if keep is None or retire is None:
            continue
        keep_agent = TravelAgent.objects.filter(user=keep).first()
        if keep_agent is None:
            continue
        retire_agent = TravelAgent.objects.filter(user=retire).first()

        if retire_agent is not None:
            moved = Reservation.objects.filter(travel_agent=retire_agent).update(travel_agent=keep_agent)
            moved += CommissionPayout.objects.filter(agent=retire_agent).update(agent=keep_agent)

            fills = {f: getattr(retire_agent, f) for f in FILL_BLANKS
                     if not getattr(keep_agent, f) and getattr(retire_agent, f)}
            carried = {}
            if moved:
                zero = Decimal("0")
                carried = {
                    "unpaid_commissions": (keep_agent.unpaid_commissions or zero)
                    + (retire_agent.unpaid_commissions or zero),
                    "pending_commissions": (keep_agent.pending_commissions or zero)
                    + (retire_agent.pending_commissions or zero),
                }
                for agent in (keep_agent, retire_agent):
                    paid = (CommissionPayout.objects.filter(agent=agent)
                            .aggregate(t=Sum("total_amount"))["t"] or zero)
                    TravelAgent.objects.filter(pk=agent.pk).update(total_paid_commission=paid)
                TravelAgent.objects.filter(pk=retire_agent.pk).update(
                    unpaid_commissions=zero, pending_commissions=zero)
            if fills or carried:
                TravelAgent.objects.filter(pk=keep_agent.pk).update(**fills, **carried)
            TravelAgent.objects.filter(pk=retire_agent.pk).update(is_active=False)

        new_name = f"{retire_name}.merged-{keep_id}"[:150]
        User.objects.filter(pk=retire.pk).update(username=new_name, is_active=False)
        log(f"  merged {retire_name} into {keep_name} (retired as {new_name})")


def refuse_if_duplicates(apps, schema_editor):
    User = apps.get_model("auth", "User")
    dupes = list(User.objects.annotate(u=Lower("username")).values("u")
                 .annotate(n=Count("id")).filter(n__gt=1).values_list("u", flat=True)[:10])
    if dupes:
        raise RuntimeError(
            "Cannot add the case-insensitive username index: these usernames exist "
            f"more than once ignoring capitals: {dupes}. Merge or rename them first.")


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0035_userprofile_shift_end_userprofile_shift_start"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("reservations", "0129_legclientmessage"),
    ]

    operations = [
        migrations.RunPython(merge_pairs, migrations.RunPython.noop),
        migrations.RunPython(refuse_if_duplicates, migrations.RunPython.noop),
        migrations.RunSQL(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON auth_user (LOWER(username));",
            f"DROP INDEX IF EXISTS {INDEX};",
        ),
    ]
