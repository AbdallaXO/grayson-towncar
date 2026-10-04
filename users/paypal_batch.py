"""PayPal / Venmo bulk payout file for travel-agent commissions.

PayPal's Payouts web upload takes a headerless CSV, one payee per row:

    recipient, amount, currency, customer id, note, wallet[, venmo feed privacy]

and pays PayPal and Venmo wallets from the same file. This module decides who
goes in the file -- owed money today, paid by PayPal or Venmo, with a handle we
can actually read -- and writes the rows.

Agents type their handle into one free-text box, so `read_recipient` pulls an
email / @handle / phone out of whatever they wrote, and refuses rather than
guesses when it can't tell: a wrong guess pays a stranger.
"""
import csv
import re
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q

WALLETS = {"paypal": "PAYPAL", "venmo": "VENMO"}
CURRENCY = "USD"
# Venmo refuses a payout without a note; PayPal shows it on the receipt.
NOTE = "Grayson Towncar commission - thank you!"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# An @handle that isn't the middle of an email address.
_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9._%+-])@([A-Za-z0-9_-]{3,30})")
_VENMO_URL_RE = re.compile(r"venmo\.com/(?:u/)?@?(?!code\b)([A-Za-z0-9_-]{3,30})", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")
# A handle typed without the @. Needs a hyphen, underscore or digit so a plain
# first name ("Joseph") is never read as somebody's @Joseph account.
_BARE_HANDLE_RE = re.compile(r"^(?=.*[-_0-9])[A-Za-z0-9_-]{3,30}$")


def _distinct(values):
    seen = {}
    for v in values:
        seen.setdefault(v.lower(), v)
    return list(seen.values())


def read_recipient(method, info):
    """Return (recipient, problem) for one payee. Exactly one of them is empty.

    `problem` is written for a dispatcher: it says what to fix on the profile.
    """
    text = (info or "").replace("​", "").strip()
    if not text:
        return "", "No payment handle saved."

    emails = _distinct(_EMAIL_RE.findall(text))

    if method == "paypal":
        if len(emails) == 1:
            return emails[0], ""
        if emails:
            return "", "More than one email saved. Keep only their PayPal email."
        return "", "PayPal needs the email on their PayPal account."

    # Venmo: a profile link or an explicit @handle is the clearest signal.
    handles = _distinct(_VENMO_URL_RE.findall(text)) or _distinct(_HANDLE_RE.findall(text))
    if len(handles) == 1:
        return f"@{handles[0]}", ""
    if handles:
        return "", "More than one Venmo handle saved. Keep only one."

    # An email they labelled PayPal isn't their Venmo login.
    if "paypal" in text.lower():
        return "", "Mentions PayPal too. Save just their Venmo @handle."
    if len(emails) == 1:
        return emails[0], ""
    if emails:
        return "", "More than one email saved. Save their Venmo @handle instead."

    phones = _distinct("".join(m) for m in _PHONE_RE.findall(text))
    if len(phones) == 1:
        return phones[0], ""
    if phones:
        return "", "More than one phone number saved. Save their Venmo @handle instead."

    if _BARE_HANDLE_RE.match(text):
        return f"@{text}", ""
    return "", "Couldn't find a Venmo @handle, email, or phone number."


@dataclass
class Payee:
    kind: str          # "agent" | "agency"
    id: int
    name: str
    method: str        # "paypal" | "venmo"
    payment_info: str  # what they typed, shown on the skipped list
    amount: Decimal
    recipient: str = ""
    problem: str = ""
    # An agent paid directly can still belong to an agency; the page links both.
    agency_id: int | None = None
    agency_name: str = ""

    @property
    def wallet(self):
        return WALLETS[self.method]

    @property
    def customer_id(self):
        # PayPal's "Customer ID": max 30 chars, no spaces. Lets a PayPal
        # receipt be traced back to the profile.
        return f"{'AGY' if self.kind == 'agency' else 'AGT'}{self.id}"


def csv_line(payee):
    line = [payee.recipient, f"{payee.amount:.2f}", CURRENCY, payee.customer_id, NOTE, payee.wallet]
    if payee.wallet == "VENMO":
        line.append("PRIVATE")  # keep commission payments off the public Venmo feed
    return line


def write_csv(payees, fh):
    writer = csv.writer(fh, lineterminator="\r\n")
    for payee in payees:
        writer.writerow(csv_line(payee))


def current_amounts(agent_ids, agency_ids, *, now=None):
    """Ready-to-pay totals right now: ({agent_id: Decimal}, {agency_id: Decimal}).

    Agency totals cover every agent whose agency handles their pay -- the same
    set Agency.process_agency_commission_payment pays, so the file never
    promises more or less than marking the agency paid will record.
    """
    from users.eligibility import bulk_ready_totals
    from users.models import TravelAgent

    children = list(
        TravelAgent.objects.filter(agency_id__in=agency_ids, agency_handles_payment=True)
        .values_list("id", "agency_id")
    )
    totals = bulk_ready_totals(set(agent_ids) | {cid for cid, _ in children}, now=now)

    agency_totals = {aid: Decimal("0") for aid in agency_ids}
    for child_id, agency_id in children:
        agency_totals[agency_id] += totals[child_id]
    return {aid: totals.get(aid, Decimal("0")) for aid in agent_ids}, agency_totals


def build_batch(*, now=None):
    """Everyone owed money who is paid by PayPal or Venmo.

    Returns (rows, skipped): rows go in the file; skipped are owed money but
    their handle couldn't be read, each with a `problem` to fix.
    """
    from users.models import Agency, TravelAgent

    agents = list(
        TravelAgent.objects.filter(is_active=True, payment_method__in=WALLETS)
        .filter(Q(agency__isnull=True) | Q(agency_handles_payment=False))
        .select_related("user", "agency")
    )
    agencies = list(Agency.objects.filter(is_active=True, payment_method__in=WALLETS))
    agent_amounts, agency_amounts = current_amounts(
        [a.id for a in agents], [a.id for a in agencies], now=now
    )

    payees = [
        Payee("agent", a.id, a.agent_name or a.user.get_username(), a.payment_method,
              a.payment_info or "", agent_amounts[a.id],
              agency_id=a.agency_id, agency_name=a.agency.name if a.agency else "")
        for a in agents
    ] + [
        Payee("agency", a.id, a.name, a.payment_method, a.payment_info or "", agency_amounts[a.id])
        for a in agencies
    ]

    rows, skipped = [], []
    for payee in sorted(payees, key=lambda p: p.name.lower()):
        if payee.amount <= 0:
            continue
        payee.recipient, payee.problem = read_recipient(payee.method, payee.payment_info)
        (skipped if payee.problem else rows).append(payee)
    return rows, skipped
