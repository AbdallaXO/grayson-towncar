"""Agency links: agents who chose to be paid through their agency, but aren't linked to it.

Lists them with a proposed agency, ticked when the name matches exactly, so a
dispatcher can link a batch in one click. Agents whose agency isn't in the
system get an "Add agency & link" button. See users.agency_links for the rules.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render

from users.agency_links import (
    CHECK, MISSING, READY, LinkRefused, agency_payable, create_agency_and_link, link_to_agency, proposals,
)
from users.models import Agency, TravelAgent


def _total(rows):
    return sum((p.owed for p in rows), Decimal("0"))


@staff_member_required
def agency_links(request):
    if request.method == "POST":
        if request.POST.get("action") == "create":
            _create(request)
        else:
            _link(request)
        return redirect("agency_links")

    found = proposals()
    for p in found:
        p.agency_problem = agency_payable(p.agency) if p.agency else ""
    groups = {status: [p for p in found if p.status == status] for status in (READY, CHECK, MISSING)}
    for rows in groups.values():
        rows.sort(key=lambda p: -p.owed)
    return render(request, "dispatching/agency_links.html", {
        "ready": groups[READY],
        "check": groups[CHECK],
        "missing": groups[MISSING],
        "ready_total": _total(groups[READY]),
        "check_total": _total(groups[CHECK]),
        "missing_total": _total(groups[MISSING]),
    })


def _link(request):
    linked, refused = [], []
    for pair in request.POST.getlist("pair"):
        try:
            agent_id, agency_id = (int(x) for x in pair.split(":"))
            agency = link_to_agency(agent_id, agency_id, user=request.user)
            linked.append(agency)
        except LinkRefused as exc:
            refused.append(str(exc))
        except (ValueError, TravelAgent.DoesNotExist, Agency.DoesNotExist):
            refused.append("One row was out of date and was skipped.")
    if linked:
        messages.success(request, f"Linked {len(linked)} agent{'s' if len(linked) != 1 else ''} to their agency. "
                                  "Their commission now goes out with the agency's payout.")
    elif not refused:
        messages.warning(request, "Nothing was ticked.")
    for reason in refused:
        messages.error(request, reason)


def _create(request):
    try:
        agency, created = create_agency_and_link(int(request.POST.get("agent", "")), user=request.user)
    except LinkRefused as exc:
        messages.error(request, str(exc))
        return
    except (ValueError, TravelAgent.DoesNotExist):
        messages.error(request, "That row was out of date. Here's a fresh list.")
        return
    if created:
        messages.success(request, f"Added {agency.name} and linked the agent. "
                                  "Add how the agency gets paid on its profile.")
    else:
        messages.success(request, f"Linked the agent to {agency.name}.")
