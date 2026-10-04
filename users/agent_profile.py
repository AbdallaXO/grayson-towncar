"""Edit a travel agent's profile from the dispatch-side agent page.

Replaces the trip to Django admin for the fields staff actually change: name,
contact details, commission rate, how the agent gets paid, and whether they're
active. Every changed field is written to the audit log with who changed it --
payment handles especially, since a changed handle is where money goes missing.
"""
from django import forms
from django.contrib.auth.models import User
from django.db import transaction

from users.models import TravelAgent
from users.paypal_batch import WALLETS, uploadable_recipient


class AgentProfileForm(forms.ModelForm):
    email = forms.EmailField(required=False, label="Email")

    class Meta:
        model = TravelAgent
        fields = ["agent_name", "phone", "commission_rate", "payment_method", "payment_info",
                  "agency_handles_payment", "is_active"]
        labels = {
            "agent_name": "Name",
            "phone": "Phone",
            "commission_rate": "Commission rate (%)",
            "payment_method": "Payment method",
            "payment_info": "Payment handle / details",
            "agency_handles_payment": "Agency pays",
            "is_active": "Active",
        }
        help_texts = {
            # The model's own hints are written to the agent ("Your full name"); staff need none there.
            "agent_name": "",
            "payment_method": "",
            "phone": "Venmo agents who gave a @handle are paid at this number.",
            "payment_info": "PayPal: the email on their PayPal account. Venmo: their @handle or phone.",
            "agency_handles_payment": "Their commission goes to their agency instead of to them.",
            "is_active": "Switched-off agents are hidden from payouts.",
        }
        widgets = {"payment_info": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].initial = self.instance.user.email
        self.fields["payment_method"].choices = [("", "— Not set —")] + list(TravelAgent.PAYMENT_METHOD_CHOICES)
        self.fields["payment_method"].required = False
        self.fields["payment_info"].required = False
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

    def save_with_audit(self, *, user):
        """Save, log each changed field, and return the list of changed labels."""
        from reservations.models import AuditLog

        agent = self.instance
        # The instance already holds the NEW values after validation; read the old ones fresh.
        before = TravelAgent.objects.select_related("user").get(pk=agent.pk)
        changed = self.changed_fields()
        if not changed:
            return []
        with transaction.atomic():
            model_fields = [f for f in changed if f != "email"]
            if model_fields:
                agent.save(update_fields=model_fields)
            if "email" in changed:
                agent.user.email = self.cleaned_data["email"]
                agent.user.save(update_fields=["email"])
            for name in changed:
                old = before.user.email if name == "email" else getattr(before, name)
                new = agent.user.email if name == "email" else getattr(agent, name)
                AuditLog.objects.create(
                    model_name="TravelAgent", object_id=agent.pk, action="updated", field_name=name,
                    old_value="" if old is None else str(old), new_value="" if new is None else str(new),
                    user=user, username=user.get_username() if user else "system",
                )
        return [str(self.fields[n].label) for n in changed]


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
    elif not info and method != "check":
        return ["No payment details are saved, so they can't be paid."]
    return []
