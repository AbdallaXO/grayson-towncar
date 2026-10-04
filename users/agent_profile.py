"""Edit a travel agent's profile from the dispatch-side agent page.

Replaces the trip to Django admin for the fields staff actually change: name,
contact details, commission rate, how the agent gets paid, and whether they're
active. Every changed field is written to the audit log with who changed it --
payment handles especially, since a changed handle is where money goes missing.
"""
import re

from django import forms
from django.contrib.auth.models import User
from django.db import transaction

from users.models import TravelAgent
from users.payment_details import mask
from users.paypal_batch import WALLETS, uploadable_recipient

# Payment fields come from users.payment_details.PaymentDetailsForm; these are
# their names in "Saved: ..." messages and the audit log.
PAYMENT_LABELS = {
    "payment_method": "Payment method",
    "payment_info": "Payment details",
    "venmo_username": "Venmo username",
    "bank_account_holder": "Name on account",
    "bank_account_type": "Account type",
    "bank_routing_number": "Routing number",
    "bank_account_number": "Account number",
    "agency": "Agency",
    "agency_handles_payment": "Agency pays",
}
# Any number of 5+ digits, even typed with dashes or spaces inside ("2670-9059-
# 12"), is shown only by its last four.
_NUMBERISH = re.compile(r"\d(?:[\d\- ]*\d)?")


def _mask_numbers(text):
    def one(m):
        digits = re.sub(r"\D", "", m.group(0))
        return f"••••{digits[-4:]}" if len(digits) >= 5 else m.group(0)
    return _NUMBERISH.sub(one, text or "")


def _for_audit(name, value, *, bank=False):
    """Audit text for a value. Account numbers are masked; so are long numbers in
    the payment notes of a bank agent. A PayPal email or Venmo phone stays readable,
    since a changed handle is exactly what the log is for."""
    if value is None:
        return ""
    if name == "bank_account_number":
        return mask(str(value))
    return _mask_numbers(str(value)) if name == "payment_info" and bank else str(value)


class AgentProfileForm(forms.ModelForm):
    email = forms.EmailField(required=False, label="Email")

    class Meta:
        model = TravelAgent
        fields = ["agent_name", "phone", "commission_rate", "agency_handles_payment", "is_active"]
        labels = {
            "agent_name": "Name",
            "phone": "Phone",
            "commission_rate": "Commission rate (%)",
            "agency_handles_payment": "Agency pays",
            "is_active": "Active",
        }
        help_texts = {
            # The model's own hints are written to the agent ("Your full name"); staff need none there.
            "agent_name": "",
            "phone": "Also used for Venmo when only a @handle is on file.",
            "agency_handles_payment": "Their commission goes to their agency instead of to them.",
            "is_active": "Switched-off agents are hidden from payouts.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].initial = self.instance.user.email
        self.fields["agent_name"].required = False
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                css = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                css = "form-select form-select-sm"
            else:
                css = "form-control form-control-sm"
            field.widget.attrs["class"] = css

    def clean_commission_rate(self):
        rate = self.cleaned_data["commission_rate"]
        if rate is None or rate < 0 or rate > 100:
            raise forms.ValidationError("Enter a rate between 0 and 100.")
        return rate

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if email and User.objects.filter(email__iexact=email).exclude(pk=self.instance.user_id).exists():
            raise forms.ValidationError("Another account already uses this email.")
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("agency_handles_payment") and not self.instance.agency_id:
            self.add_error("agency_handles_payment",
                           "Link them to an agency first (Agency Assignment, on the right).")
        return cleaned

    def changed_fields(self):
        names = [f for f in self.changed_data if f != "email"]
        if (self.cleaned_data.get("email") or "") != (self.instance.user.email or ""):
            names.append("email")
        return names

    def save_with_audit(self, *, user, pay=None):
        """Save, log each changed field, and return the list of changed labels.

        `pay` is a validated PaymentDetailsForm; its changes are saved and
        logged alongside. Staff set "Agency pays" themselves, so picking
        "Agency" here never links an agency on its own.
        """
        from reservations.models import AuditLog

        agent = self.instance
        # The instance already holds the NEW values after validation; read the old ones fresh.
        before = TravelAgent.objects.select_related("user").get(pk=agent.pk)
        changed = self.changed_fields()
        if pay is not None:
            changed += [f for f in pay.apply(agent, link_agency=False) if f not in changed]
        if not changed:
            return []
        with transaction.atomic():
            model_fields = [f for f in changed if f != "email"]
            if model_fields:
                agent.save(update_fields=model_fields)
            if "email" in changed:
                agent.user.email = self.cleaned_data["email"]
                agent.user.save(update_fields=["email"])
            bank = "bank" in (before.payment_method, agent.payment_method)
            for name in changed:
                old = before.user.email if name == "email" else getattr(before, name)
                new = agent.user.email if name == "email" else getattr(agent, name)
                AuditLog.objects.create(
                    model_name="TravelAgent", object_id=agent.pk, action="updated", field_name=name,
                    old_value=_for_audit(name, old, bank=bank), new_value=_for_audit(name, new, bank=bank),
                    user=user, username=user.get_username() if user else "system",
                )
        return [str(self.fields[n].label) if n in self.fields else PAYMENT_LABELS.get(n, n) for n in changed]


def payout_warnings(agent):
    """Plain-English reasons this agent can't be paid as saved, or []."""
    if agent.agency_handles_payment and agent.agency_id:
        agency = agent.agency
        if not agency.payment_method or agency.payment_method == "agency":
            return [f"{agency.name} has no payment method yet, so this agent's commission can't go out."]
        return []
    method, info = agent.payment_method or "", (agent.payment_info or "").strip()
    if not method:
        return ["No payment method is set, so they can't be paid."]
    if method == "agency":
        return ["They chose \"Agency\", but no agency pays them. Link their agency and turn on "
                "\"Agency pays\", or pick how they're paid."]
    if method in WALLETS:
        if not info:
            return ["No payment handle is saved, so they can't be paid."]
        _, problem, _ = uploadable_recipient(method, info, agent.phone)
        if problem:
            return [f"They'll be left out of the PayPal & Venmo batch: {problem}"]
    elif method == "bank" and not (agent.bank_routing_number and agent.bank_account_number):
        return ["No routing and account number are saved, so they can't be paid by bank transfer."
                + (" (Their old notes may have them; re-enter them in the bank fields.)" if info else "")]
    elif not info and method != "check":
        return ["No payment details are saved, so they can't be paid."]
    return []
