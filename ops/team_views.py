"""
The Team page — the lead's desk: what needs a decision, and what every
dispatcher is doing today. Data assembly lives in ops/team_today.py.

Access follows the Dispatch Lead group: ops.view_team opens the page (an admin
always can), and the refunds card shows only to whoever can approve refunds.
Nothing here writes; every action links to the page where it already happens.
"""

from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render

from . import team_today as team
from .services import auto_close_stale_shifts
from .views import _can_view_team


@login_required(login_url="login")
@user_passes_test(_can_view_team, login_url="dashboard")
def team_today(request):
    # The same lazy tidy-up the Time Clock pages run (this app has no
    # scheduler), so a shift nobody clocked out of days ago never reads as
    # "Working since 8:22 AM".
    auto_close_stale_shifts()
    can_approve = request.user.is_superuser or request.user.has_perm("reservations.approve_refund")
    context = team.build(include_refunds=can_approve)
    return render(request, "dispatching/team_today.html", context)
