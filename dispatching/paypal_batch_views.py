"""PayPal & Venmo batch: one file for PayPal's bulk upload, then one click to
record those payees paid.

The page signs a snapshot of exactly who it listed, where the money goes, and
how much. Download and Mark-paid both work from that snapshot, and both refuse
a payee whose amount has moved since -- so the file PayPal sends and the
payouts the app records are the same list at the same amounts.
"""
import io
from decimal import Decimal

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from users.paypal_batch import Payee, build_batch, write_csv
from users.services import process_bulk_payouts

_SALT = "dispatching.paypal_batch"
# Long enough to finish the upload in PayPal and come back; short enough that
# yesterday's tab can't record today's payouts.
_MAX_AGE = 60 * 60 * 12


# Who one batch pays. "agents" are the ones paid directly -- the same set as the
# Direct Agents tab; an agent whose agency collects their pay rides on the agency.
_GROUPS = {
    "all": ("Everyone", None),
    "agents": ("Agents", "agent"),
    "agencies": ("Agencies", "agency"),
}


def _group(value):
    return value if value in _GROUPS else "all"


def _back(group):
    url = reverse("paypal_batch")
    return redirect(url if group == "all" else f"{url}?who={group}")


def _sign(rows, made_at, group):
    return signing.dumps(
        {
            "at": made_at,
            "who": group,
            "rows": [[r.kind, r.id, r.name, r.method, r.recipient, f"{r.amount:.2f}"] for r in rows],
        },
        salt=_SALT,
        compress=True,
    )


def _unsign(token):
    try:
        data = signing.loads(token or "", salt=_SALT, max_age=_MAX_AGE)
    except signing.BadSignature:  # also covers SignatureExpired
        return None
    data["rows"] = [
        Payee(kind, pk, name, method, "", Decimal(amount), recipient=recipient)
        for kind, pk, name, method, recipient, amount in data["rows"]
    ]
    return data


@staff_member_required
def paypal_batch(request):
    if request.method == "POST":
        data = _unsign(request.POST.get("token"))
        if data is None:
            messages.warning(request, "That list had expired. Here's a fresh one.")
            return _back(_group(request.POST.get("who")))
        if request.POST.get("action") == "download":
            return _download(request, data)
        if request.POST.get("action") == "mark_paid":
            return _mark_paid(request, data)
        return _back(data["who"])

    group = _group(request.GET.get("who"))
    every_row, every_skipped = build_batch()
    kind = _GROUPS[group][1]
    rows = [r for r in every_row if kind in (None, r.kind)]
    skipped = [r for r in every_skipped if kind in (None, r.kind)]

    groups = []
    for key, (label, group_kind) in _GROUPS.items():
        members = [r for r in every_row if group_kind in (None, r.kind)]
        groups.append({
            "key": key,
            "label": label,
            "count": len(members),
            "total": sum((r.amount for r in members), Decimal("0")),
            "active": key == group,
        })

    made_at = timezone.localtime().strftime("%Y-%m-%d %H:%M")
    return render(request, "dispatching/paypal_batch.html", {
        "rows": rows,
        "skipped": skipped,
        "groups": groups,
        "who": group,
        "who_label": _GROUPS[group][0],
        "total": sum((r.amount for r in rows), Decimal("0")),
        "paypal_count": sum(1 for r in rows if r.method == "paypal"),
        "venmo_count": sum(1 for r in rows if r.method == "venmo"),
        "skipped_total": sum((r.amount for r in skipped), Decimal("0")),
        "token": _sign(rows, made_at, group),
        "made_at": made_at,
    })


def _download(request, data):
    current, _ = build_batch()
    now = {(r.kind, r.id): (r.recipient, r.amount) for r in current}
    if any(now.get((r.kind, r.id)) != (r.recipient, r.amount) for r in data["rows"]):
        messages.warning(
            request,
            "Someone's amount or handle changed since you opened this page. "
            "The list below is up to date. Check it, then download again.",
        )
        return _back(data["who"])

    buffer = io.StringIO()
    write_csv(data["rows"], buffer)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    stamp = data["at"].replace(" ", "-").replace(":", "")
    who = "" if data["who"] == "all" else f"{data['who']}-"
    response["Content-Disposition"] = f'attachment; filename="paypal-payouts-{who}{stamp}.csv"'
    return response


def _mark_paid(request, data):
    reference = f"PayPal batch {data['at']}"
    results = process_bulk_payouts(
        [
            {
                "type": r.kind,
                "id": r.id,
                "method": r.method,
                "reference": reference,
                "expected_amount": f"{r.amount:.2f}",
            }
            for r in data["rows"]
        ],
        sent_by=request.user,
    )

    paid = [res for res in results if res.get("ok")]
    if paid:
        total = sum((Decimal(res["amount"]) for res in paid), Decimal("0"))
        messages.success(request, f"Marked {len(paid)} paid, ${total:,.2f} in all.")

    names = {(r.kind, r.id): r.name for r in data["rows"]}
    for res in results:
        if res.get("ok"):
            continue
        name = names.get((res.get("type"), res.get("id")), "A payee")
        if "owed_now" in res:
            # PayPal already sent the file's amount; the app still shows the
            # whole balance as owed. Say exactly how to square it.
            sent, owed = Decimal(res["expected_amount"]), Decimal(res["owed_now"])
            if owed > sent:
                fix = (f"If PayPal sent the ${sent:,.2f}, send them the other ${owed - sent:,.2f}, "
                       f"then mark them paid from Affiliate Management.")
            else:
                fix = "Check what they're owed before marking them paid from Affiliate Management."
            messages.error(
                request,
                f"{name} was not marked paid. The file had ${sent:,.2f}, "
                f"but they're now owed ${owed:,.2f}. {fix}",
            )
        else:
            messages.error(request, f"{name} was not marked paid. {res.get('error', '')}".strip())
    return _back(data["who"])
