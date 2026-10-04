"""Move bank numbers out of old free-text payment notes into the bank fields.

Agents on bank transfer typed their routing and account numbers into one
plain-text box before users 0037. For each bank agent with no structured
numbers yet, this reads the note and -- only when it holds exactly one valid
routing number and exactly one other account-length number -- moves them into
bank_routing_number / bank_account_number and replaces the note with the
masked one-line summary ("Checking ••••6789 · Jane Doe"). The old note goes to
the audit log with its long numbers masked.

Anything less clear is left exactly as it was, for staff to re-enter: the
agent's profile already says so. A wrong guess would send commission to a
stranger's account.

The reading rules are copied here rather than imported from
users.bank_notes, so this migration keeps working if that module changes.
Historical models and QuerySet.update() only: no signals, no emails.
"""
import re

from django.db import migrations

_NUMBER_RE = re.compile(r"(?<![\d-])\d+(?:-\d+)*(?![\d])")
# Any number of 5+ digits, even typed with dashes or spaces inside ("2670-9059-
# 12"), is shown only by its last four.
_NUMBERISH = re.compile(r"\d(?:[\d\- ]*\d)?")


def _mask_numbers(text):
    def one(m):
        digits = re.sub(r"\D", "", m.group(0))
        return f"••••{digits[-4:]}" if len(digits) >= 5 else m.group(0)
    return _NUMBERISH.sub(one, text or "")


def _routing_ok(number):
    if not re.fullmatch(r"\d{9}", number):
        return False
    d = [int(c) for c in number]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def _read(text):
    numbers = []
    for raw in _NUMBER_RE.findall(text or ""):
        digits = re.sub(r"\D", "", raw)
        if 4 <= len(digits) <= 17 and digits not in numbers:
            numbers.append(digits)
    routing = [n for n in numbers if len(n) == 9 and _routing_ok(n)]
    if len(routing) != 1:
        return None
    accounts = [n for n in numbers if n != routing[0]]
    if len(accounts) != 1:
        return None
    lowered = (text or "").lower()
    kinds = [k for k in ("checking", "savings") if k in lowered]
    return routing[0], accounts[0], kinds[0] if len(kinds) == 1 else ""


def convert(apps, schema_editor):
    TravelAgent = apps.get_model("users", "TravelAgent")
    AuditLog = apps.get_model("reservations", "AuditLog")

    for agent in TravelAgent.objects.filter(payment_method="bank", bank_account_number=""):
        found = _read(agent.payment_info)
        if not found:
            continue
        routing, account, kind = found
        holder = (agent.agent_name or "").strip()
        label = {"checking": "Checking", "savings": "Savings"}.get(kind, "Bank")
        summary = f"{label} ••••{account[-4:]}" + (f" · {holder}" if holder else "")
        TravelAgent.objects.filter(pk=agent.pk, bank_account_number="").update(
            bank_routing_number=routing,
            bank_account_number=account,
            bank_account_type=kind,
            bank_account_holder=holder,
            payment_info=summary,
        )
        AuditLog.objects.create(
            model_name="TravelAgent",
            object_id=agent.pk,
            action="updated",
            field_name="payment_info",
            old_value=_mask_numbers(agent.payment_info),
            new_value=summary,
            username="system (bank notes cleanup)",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0037_travelagent_structured_payout_details"),
        ("reservations", "0130_refundrequest_approve_permission"),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
