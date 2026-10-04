"""Read bank numbers out of the old free-text payment notes -- only when it's unambiguous.

Before users.payment_details, agents typed bank details into one box ("Chase
routing 021000021 acct 000123456789 checking"). read_bank_note() pulls the
routing and account number out when there is exactly one valid routing number
and exactly one other account-length number. Anything else returns a reason
instead, for a person to re-enter: a wrong guess here sends commission to a
stranger's account.
"""
import re

from users.payment_details import routing_number_ok

# Digit runs, joined across single dashes ("0210-00021"). Not across spaces:
# "021000021 12345678" is two numbers, and a spaced-out account ("1234 5678
# 9012") reads as several candidates, which sends that note to a person.
_NUMBER_RE = re.compile(r"(?<![\d-])\d+(?:-\d+)*(?![\d])")


def _numbers(text):
    out = []
    for raw in _NUMBER_RE.findall(text or ""):
        digits = re.sub(r"\D", "", raw)
        if 4 <= len(digits) <= 17:
            out.append(digits)
    return out


def read_bank_note(text):
    """({"routing", "account", "type"}, "") when clear-cut, else (None, why)."""
    numbers = list(dict.fromkeys(_numbers(text)))  # distinct, in order
    routing = [n for n in numbers if len(n) == 9 and routing_number_ok(n)]
    if not routing:
        return None, "No valid routing number in the note."
    if len(routing) > 1:
        return None, "More than one number looks like a routing number."
    accounts = [n for n in numbers if n != routing[0]]
    if not accounts:
        return None, "No account number in the note."
    if len(accounts) > 1:
        return None, "More than one number could be the account number."
    lowered = (text or "").lower()
    kinds = [k for k in ("checking", "savings") if k in lowered]
    return {"routing": routing[0], "account": accounts[0], "type": kinds[0] if len(kinds) == 1 else ""}, ""
