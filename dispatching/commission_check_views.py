"""Commission check: possible personal trips and duplicate bookings, decided one click at a time.

See users.commission_check for what gets flagged. Nothing here changes a
booking until a person clicks.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from django.urls import reverse

from reservations.models import Reservation
from users.commission_check import (
    CheckRefused, duplicate_groups, exclude, mark_fine, personal_suspects, recent_decisions, undo,
)
from users.eligibility import STATUS_READY
from users.models import CommissionCheck

TABS = ("personal", "duplicates", "decided")
PERSONAL_REASON = "Personal trip — non-commissionable"


def _ids(request, name):
    try:
        return [int(x) for x in request.POST.getlist(name)]
    except ValueError:
        return []


@staff_member_required
def commission_check(request):
    tab = request.GET.get("tab") if request.GET.get("tab") in TABS else "personal"
    if request.method == "POST":
        tab = request.POST.get("tab") if request.POST.get("tab") in TABS else tab
        _decide(request)
        return redirect(f"{reverse('commission_check')}?tab={tab}")

    personal = personal_suspects()
    duplicates = duplicate_groups()
    decided = recent_decisions()
    return render(request, "dispatching/commission_check.html", {
        "tab": tab,
        "personal": personal,
        "duplicates": duplicates,
        "decided": decided,
        "personal_now": sum((f.commission for f in personal if f.status == STATUS_READY), Decimal("0")),
        "personal_total": sum((f.commission for f in personal), Decimal("0")),
        "dup_now": sum((g.payable_now for g in duplicates), 0),
        "dup_total": sum((g.commission for g in duplicates), Decimal("0")),
    })


def _number(res_id):
    return f"#50{res_id}"


def _decide(request):
    action = request.POST.get("action", "")
    if action == "undo":
        try:
            res = undo(int(request.POST.get("check", "")), user=request.user)
        except ValueError:
            messages.warning(request, "That row was out of date. Here's a fresh list.")
        except CheckRefused as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Undone. #{res.display_number} is back on the list to decide again.")
        return
    ids = _ids(request, "reservation")
    if not ids:
        messages.warning(request, "That row was out of date. Here's a fresh list.")
        return
    try:
        if action == "personal_exclude":
            exclude(ids[0], kind=CommissionCheck.PERSONAL, reason=PERSONAL_REASON, user=request.user)
            messages.success(request, f"{_number(ids[0])} is now not commissionable. It won't be paid. (Undo it under Decided.)")
        elif action == "personal_fine":
            mark_fine(ids[:1], kind=CommissionCheck.PERSONAL, user=request.user)
            messages.success(request, f"{_number(ids[0])} will be paid as normal and won't be flagged again.")
        elif action == "dup_exclude":
            keep = [i for i in _ids(request, "group") if i != ids[0]]
            reason = "Duplicate of " + ", ".join(_number(i) for i in keep) if keep else "Duplicate booking"
            exclude(ids[0], kind=CommissionCheck.DUPLICATE, reason=reason, user=request.user)
            if keep:
                mark_fine(keep, kind=CommissionCheck.DUPLICATE, user=request.user)
            messages.success(request, f"{_number(ids[0])} won't be paid. "
                                      f"{', '.join(_number(i) for i in keep) or 'The other booking'} still will.")
        elif action == "dup_fine":
            mark_fine(_ids(request, "group"), kind=CommissionCheck.DUPLICATE, user=request.user)
            messages.success(request, "Marked as separate bookings. Each one will be paid.")
        else:
            messages.warning(request, "Nothing was changed.")
    except CheckRefused as exc:
        messages.error(request, str(exc))
    except Reservation.DoesNotExist:
        messages.error(request, "That booking no longer exists. Here's a fresh list.")
