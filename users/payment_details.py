"""How a travel agent gets paid, collected as checked fields instead of free text.

Sign-up, the agent's own profile and the staff editor all use PaymentDetailsForm,
so whatever gets saved is something we can actually pay:

    PayPal -> the email on their PayPal account
    Venmo  -> the phone on their Venmo (PayPal's bulk upload can't pay a @handle);
              the @username is optional, kept so staff can see who's who
    Zelle  -> an email or US phone
    Bank   -> holder, checking/savings, a real routing number, the account number twice
    Agency -> nothing to enter; we link them to their agency when its name matches

Cash App, Check and Other are no longer offered. An agent already on one keeps
it (and its free-text details) until they pick something else.

payment_info stays the one-line summary the rest of the app reads (the PayPal
batch, Affiliate Management, statements). For a bank it holds only the last four
digits; the full numbers live in their own fields and are shown masked.
"""
import re

from django import forms

from users.paypal_batch import read_recipient

OFFERED = [
    ("paypal", "PayPal"),
    ("venmo", "Venmo"),
    ("zelle", "Zelle"),
    ("bank", "Bank transfer"),
    ("agency", "My agency pays me"),
]
RETIRED = {"cashapp": "Cash App", "check": "Check", "other": "Other"}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,30}$")


def us_phone(raw):
    """10-digit US number from whatever was typed, or ''."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def format_phone(digits):
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def routing_number_ok(number):
    """ABA routing checksum: 3-7-1 weights over nine digits, total divisible by 10."""
    if not re.fullmatch(r"\d{9}", number or ""):
        return False
    d = [int(c) for c in number]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def mask(number):
    return f"••••{number[-4:]}" if number else ""


class PaymentDetailsForm(forms.Form):
    payment_method = forms.ChoiceField(label="How should we pay you?", required=False)
    paypal_email = forms.EmailField(label="PayPal email", required=False,
                                    help_text="The email on your PayPal account.")
    venmo_phone = forms.CharField(label="Phone number on your Venmo", required=False,
                                  help_text="We send your Venmo payments to this number.")
    venmo_username = forms.CharField(label="Venmo username (optional)", required=False,
                                     help_text="Like @Jane-Doe-7.")
    zelle_contact = forms.CharField(label="Zelle email or phone", required=False)
    bank_account_holder = forms.CharField(label="Name on the account", required=False)
    bank_account_type = forms.ChoiceField(
        label="Account type", required=False,
        choices=[("", "Choose…"), ("checking", "Checking"), ("savings", "Savings")],
    )
    bank_routing_number = forms.CharField(label="Routing number", required=False,
                                          help_text="9 digits, bottom-left of a check.")
    bank_account_number = forms.CharField(label="Account number", required=False)
    bank_account_number_confirm = forms.CharField(label="Account number again", required=False)
    legacy_details = forms.CharField(label="Payment details", required=False,
                                     widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, agent=None, require_method=True, input_css="form-control",
                 select_css="form-select", **kwargs):
        kwargs.setdefault("prefix", "pay")
        self.agent = agent
        self.require_method = require_method
        current = (agent.payment_method or "") if agent else ""
        kwargs.setdefault("initial", self._initial_from(agent))
        super().__init__(*args, **kwargs)

        choices = [("", "Choose…")] + OFFERED
        if current in RETIRED:  # keep what they have; they just can't pick it fresh
            choices.append((current, f"{RETIRED[current]} (no longer offered)"))
        self.fields["payment_method"].choices = choices
        self.retired_method = current if current in RETIRED else ""

        for field in self.fields.values():
            is_select = isinstance(field.widget, forms.Select)
            field.widget.attrs["class"] = select_css if is_select else input_css
        self.fields["bank_routing_number"].widget.attrs.update(inputmode="numeric", autocomplete="off")
        for name in ("bank_account_number", "bank_account_number_confirm"):
            self.fields[name].widget.attrs.update(inputmode="numeric", autocomplete="off")
        self.fields["venmo_phone"].widget.attrs.update(inputmode="tel", autocomplete="tel")

    @staticmethod
    def _initial_from(agent):
        """Pre-fill from what's saved, including best-effort reads of old free text."""
        if agent is None:
            return {}
        method, info = agent.payment_method or "", (agent.payment_info or "").strip()
        initial = {"payment_method": method}
        if method == "paypal":
            initial["paypal_email"] = read_recipient("paypal", info)[0]
        elif method == "venmo":
            # A @handle-only record is already paid at the profile phone (see
            # paypal_batch.uploadable_recipient), so offer that number to confirm.
            phone = us_phone(info) or us_phone(agent.phone)
            initial["venmo_phone"] = format_phone(phone) if phone else ""
            handle = agent.venmo_username or (read_recipient("venmo", info)[0] if "@" in info else "")
            initial["venmo_username"] = handle if handle.startswith("@") or not handle else f"@{handle}"
        elif method == "zelle":
            initial["zelle_contact"] = info
        elif method == "bank":
            initial.update(
                bank_account_holder=agent.bank_account_holder,
                bank_account_type=agent.bank_account_type,
                bank_routing_number=agent.bank_routing_number,
                bank_account_number=agent.bank_account_number,
                bank_account_number_confirm=agent.bank_account_number,
            )
        elif method in RETIRED:
            initial["legacy_details"] = info
        return initial

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("payment_method") or ""
        need = self.add_error

        if not method:
            if self.require_method:
                need("payment_method", "Choose how you'd like to be paid.")
            return cleaned

        if method == "paypal" and not cleaned.get("paypal_email"):
            if "paypal_email" not in self.errors:
                need("paypal_email", "Enter the email on your PayPal account.")

        if method == "venmo":
            phone = us_phone(cleaned.get("venmo_phone"))
            if not phone:
                need("venmo_phone", "Enter the 10-digit US phone number on your Venmo.")
            cleaned["venmo_phone"] = phone
            username = (cleaned.get("venmo_username") or "").strip().lstrip("@")
            if username and not _USERNAME_RE.match(username):
                need("venmo_username", "Letters, numbers, - and _ only, like @Jane-Doe-7.")
            cleaned["venmo_username"] = username

        if method == "zelle":
            contact = (cleaned.get("zelle_contact") or "").strip()
            email = read_recipient("paypal", contact)[0]  # same email rules
            phone = us_phone(contact)
            if email and email.lower() == contact.lower():
                cleaned["zelle_contact"] = email
            elif phone:
                cleaned["zelle_contact"] = format_phone(phone)
            else:
                need("zelle_contact", "Enter the email or US phone number on your Zelle.")

        if method == "bank":
            if not (cleaned.get("bank_account_holder") or "").strip():
                need("bank_account_holder", "Enter the name on the account.")
            if not cleaned.get("bank_account_type"):
                need("bank_account_type", "Choose checking or savings.")
            routing = re.sub(r"\D", "", cleaned.get("bank_routing_number") or "")
            if not routing_number_ok(routing):
                need("bank_routing_number", "That isn't a valid 9-digit US routing number.")
            account = re.sub(r"\D", "", cleaned.get("bank_account_number") or "")
            confirm = re.sub(r"\D", "", cleaned.get("bank_account_number_confirm") or "")
            if not 4 <= len(account) <= 17:
                need("bank_account_number", "Account numbers are 4 to 17 digits.")
            elif account != confirm:
                need("bank_account_number_confirm", "The two account numbers don't match.")
            cleaned["bank_routing_number"], cleaned["bank_account_number"] = routing, account

        return cleaned

    def summary(self):
        """The one-line payment_info for what was entered."""
        c = self.cleaned_data
        method = c.get("payment_method") or ""
        if method == "paypal":
            return c["paypal_email"]
        if method == "venmo":
            return format_phone(c["venmo_phone"])
        if method == "zelle":
            return c["zelle_contact"]
        if method == "bank":
            kind = dict(checking="Checking", savings="Savings")[c["bank_account_type"]]
            return f"{kind} {mask(c['bank_account_number'])} · {c['bank_account_holder'].strip()}"
        if method in RETIRED:
            return (c.get("legacy_details") or "").strip()
        return ""

    def apply(self, agent, *, link_agency=True):
        """Write the cleaned details onto `agent` (not saved). Returns changed field names.

        Picking any method other than bank clears the stored bank numbers -- no
        reason to keep someone's account number once we don't pay into it.
        """
        from users.models import Agency

        c = self.cleaned_data
        method = c.get("payment_method") or ""
        bank = method == "bank"
        values = {
            "payment_method": method or None,
            "payment_info": self.summary(),
            "venmo_username": c.get("venmo_username", "") if method == "venmo" else "",
            "bank_account_holder": c["bank_account_holder"].strip() if bank else "",
            "bank_account_type": c["bank_account_type"] if bank else "",
            "bank_routing_number": c["bank_routing_number"] if bank else "",
            "bank_account_number": c["bank_account_number"] if bank else "",
        }
        if method == "agency" and link_agency:
            if agent.agency_id:
                values["agency_handles_payment"] = True
            else:
                typed = (agent.agency_name or "").strip()
                match = Agency.objects.filter(name__iexact=typed, is_active=True) if typed else Agency.objects.none()
                if match.count() == 1:
                    values["agency"] = match.first()
                    values["agency_handles_payment"] = True

        changed = []
        for name, value in values.items():
            current = getattr(agent, name)
            same = current == value if isinstance(value, (bool, Agency)) else (current or "") == (value or "")
            if not same:
                setattr(agent, name, value)
                changed.append(name)
        return changed
