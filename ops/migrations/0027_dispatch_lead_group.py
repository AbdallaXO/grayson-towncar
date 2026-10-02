"""Create the "Dispatch Lead" group: everything a shift lead needs, nothing more.

Founder decision 2026-10-02: a dispatcher promoted to team lead keeps dispatching,
leads the floor during their shift, approves refunds, and can see the team. The
lead is NOT made a superuser — that would also hand them revenue, profit,
payroll, pay rates, commissions and the Django admin. This group is the tier in
between that docs/dispatch-ops/SHIFT-SYSTEM-AUDIT.md (decision 20) described and
never created.

What the group carries:
  * approve or reject refund requests, their own included — correcting a refund
    that already went through stays superuser-only;
  * see who is on the clock right now and the staffing board, look only;
  * review how shifts ran, reopen a finished checklist, hand a shift note to
    someone else;
  * remove a "keep an eye on it" watch flag, with a reason.

Only custom permissions, never a model's add/change/delete: those would open
editing screens in the Django admin, which uses the default site.

Nobody is added here. Membership is set in the admin — Users, open the person,
Groups, "Dispatch Lead" — and taken away the same way, without a deploy.

Permission rows are normally created by post_migrate, after every migration has
run, so the two this group needs that were declared moments ago (0026 here,
reservations 0130) would not exist yet. They are created first. A permission
that still cannot be found stops the deploy in words rather than shipping a lead
who cannot do the job.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations
from django.db.models import Q

GROUP = "Dispatch Lead"

PERMISSIONS = [
    ("reservations", "approve_refund"),
    ("reservations", "remove_keoi"),
    ("ops", "view_team"),
    ("ops", "review_checklists"),
    ("ops", "reopen_checklist"),
    ("ops", "edit_exception_owner"),
]


def create_dispatch_lead_group(apps, schema_editor):
    for app_label in sorted({app for app, _ in PERMISSIONS}):
        app_config = apps.get_app_config(app_label)
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    wanted = Q()
    for app_label, codename in PERMISSIONS:
        wanted |= Q(content_type__app_label=app_label, codename=codename)
    perms = list(Permission.objects.filter(wanted).select_related("content_type"))

    missing = set(PERMISSIONS) - {(p.content_type.app_label, p.codename) for p in perms}
    if missing:
        raise RuntimeError(
            "Cannot build the Dispatch Lead group — these permissions do not exist: "
            + ", ".join(f"{app}.{code}" for app, code in sorted(missing))
        )

    group, _ = Group.objects.get_or_create(name=GROUP)
    group.permissions.add(*perms)


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("ops", "0026_shiftchecklist_view_team_permission"),
        ("reservations", "0130_refundrequest_approve_permission"),
    ]

    operations = [
        migrations.RunPython(create_dispatch_lead_group, migrations.RunPython.noop),
    ]
