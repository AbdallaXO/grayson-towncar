"""PayPal & Venmo batch: one file for PayPal's bulk upload, then one click to
record those payees paid.

The page signs a snapshot of exactly who it listed, where the money goes, and
how much. The operator narrows it (agents / agencies, PayPal / Venmo) and ticks
who to include. Download writes the ticked rows; Mark-paid records them -- but
only when the ticked rows exactly match a file this browser downloaded, so what
PayPal sends and what the app records can't drift apart. Both also refuse a
payee whose amount has moved since the page was loaded.
"""
import hashlib
import io
from decimal import Decimal

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from users.paypal_batch import Payee, build_batch, write_csv
from users.services import process_bulk_payouts

_SALT = "dispatching.paypal_batch"
_FILES_SALT = "dispatching.paypal_batch.files"
_FILES_COOKIE = "pb_files"
_FILES_KEPT = 10  # e.g. an Agents file and an Agencies file downloaded the same morning
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
_WALLETS = {
    "both": ("PayPal & Venmo", None),
    "paypal": ("PayPal only", "paypal"),
    "venmo": ("Venmo only", "venmo"),
}


def _pick(value, choices, default):
    return value if value in choices else default


def _back(who, wallet):
    params = {k: v for k, v in (("who", who), ("wallet", wallet)) if v not in ("all", "both")}
    url = reverse("paypal_batch")
    return redirect(f"{url}?{urlencode(params)}" if params else url)


def _key(row):
    return f"{row.kind}:{row.id}"


def _fingerprint(rows):
    """Stable id for exactly these payees, handles and amounts."""
    lines = sorted(f"{r.kind}:{r.id}:{r.recipient}:{r.amount:.2f}" for r in rows)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:20]


def _downloaded(request):
    try:
        return signing.loads(request.COOKIES.get(_FILES_COOKIE, ""), salt=_FILES_SALT, max_age=_MAX_AGE)
    except signing.BadSignature:
        return []


def _sign(rows, made_at, who, wallet):
    return signing.dumps(
        {
            "at": made_at,
            "who": who,
            "wallet": wallet,
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
    data.setdefault("wallet", "both")
    return data


def _counts(rows):
    return {"count": len(rows), "total": sum((r.amount for r in rows), Decimal("0"))}


@staff_member_required
def paypal_batch(request):
    if request.method == "POST":
        data = _unsign(request.POST.get("token"))
        if data is None:
            messages.warning(request, "That list had expired. Here's a fresh one.")
            return _back(_pick(request.POST.get("who"), _GROUPS, "all"),
                         _pick(request.POST.get("wallet"), _WALLETS, "both"))
        # Only ticked rows, and only rows this page actually listed.
        picks = set(request.POST.getlist("pick"))
        data["rows"] = [r for r in data["rows"] if _key(r) in picks]
        if not data["rows"]:
            messages.warning(request, "Nobody is ticked.")
            return _back(data["who"], data["wallet"])
        if request.POST.get("action") == "download":
            return _download(request, data)
        if request.POST.get("action") == "mark_paid":
            return _mark_paid(request, data)
        return _back(data["who"], data["wallet"])

    who = _pick(request.GET.get("who"), _GROUPS, "all")
    wallet = _pick(request.GET.get("wallet"), _WALLETS, "both")
    every_row, every_skipped = build_batch()

    def keep(r, who_key=who, wallet_key=wallet):
        kind, method = _GROUPS[who_key][1], _WALLETS[wallet_key][1]
        return kind in (None, r.kind) and method in (None, r.method)

    rows = [r for r in every_row if keep(r)]
    skipped = [r for r in every_skipped if keep(r)]

    # Each switch shows what it would list given the other switch's current choice.
    groups = [dict(key=k, label=label, active=k == who, **_counts([r for r in every_row if keep(r, who_key=k)]))
              for k, (label, _) in _GROUPS.items()]
    wallets = [dict(key=k, label=label, active=k == wallet, **_counts([r for r in every_row if keep(r, wallet_key=k)]))
               for k, (label, _) in _WALLETS.items()]

    def switch_url(**change):
        params = {"who": who, "wallet": wallet, **change}
        params = {k: v for k, v in params.items() if v not in ("all", "both")}
        return f"{reverse('paypal_batch')}?{urlencode(params)}" if params else reverse("paypal_batch")

    for g in groups:
        g["url"] = switch_url(who=g["key"])
    for w in wallets:
        w["url"] = switch_url(wallet=w["key"])

    made_at = timezone.localtime().strftime("%Y-%m-%d %H:%M")
    narrowed = [label for label, on in ((_GROUPS[who][0], who != "all"), (_WALLETS[wallet][0], wallet != "both")) if on]
    return render(request, "dispatching/paypal_batch.html", {
        "rows": rows,
        "skipped": skipped,
        "groups": groups,
        "wallets": wallets,
        "who": who,
        "wallet": wallet,
        "narrowed": " · ".join(narrowed),
        "total": sum((r.amount for r in rows), Decimal("0")),
        "paypal_count": sum(1 for r in rows if r.method == "paypal"),
        "venmo_count": sum(1 for r in rows if r.method == "venmo"),
        "skipped_total": sum((r.amount for r in skipped), Decimal("0")),
        "token": _sign(rows, made_at, who, wallet),
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
        return _back(data["who"], data["wallet"])

    buffer = io.StringIO()
    write_csv(data["rows"], buffer)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    stamp = data["at"].replace(" ", "-").replace(":", "")
    parts = [p for p in (data["who"], data["wallet"]) if p not in ("all", "both")]
    label = "".join(f"{p}-" for p in parts)
    response["Content-Disposition"] = (
        f'attachment; filename="paypal-payouts-{label}{len(data["rows"])}-{stamp}.csv"'
    )
    # Remember what went into this file, so Mark-paid can insist on the same set.
    files = ([fp for fp in _downloaded(request) if fp != _fingerprint(data["rows"])]
             + [_fingerprint(data["rows"])])[-_FILES_KEPT:]
    response.set_cookie(
        _FILES_COOKIE, signing.dumps(files, salt=_FILES_SALT), max_age=_MAX_AGE,
        path=reverse("paypal_batch"), httponly=True, samesite="Lax",
    )
    return response


def _mark_paid(request, data):
    if _fingerprint(data["rows"]) not in _downloaded(request):
        messages.error(
            request,
            "Nothing was marked paid. The people ticked don't match a file you downloaded. "
            "Tick exactly who was in the file you sent through PayPal, or download a new file "
            "for these people and send that one.",
        )
        return _back(data["who"], data["wallet"])

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
    return _back(data["who"], data["wallet"])
