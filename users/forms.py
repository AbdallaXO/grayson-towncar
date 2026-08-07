import logging
import time

from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django import forms
from django.core.exceptions import ValidationError
from .models import PartnerForm, ContactUsForm
from .spam import BLOCK_THRESHOLD, score_submission

logger = logging.getLogger(__name__)


class CustomUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ["username", "email", "password1", "password2"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields:
            self.fields[field].widget.attrs.update(
                {
                    "class": "input",
                }
            )
        # Clean labels
        self.fields["password1"].label = "Password"
        self.fields["password2"].label = "Confirm Password"

        # Remove djangos default help text clutter with that form
        self.fields["username"].help_text = ""
        self.fields["password1"].help_text = ""
        self.fields["password2"].help_text = ""

        self.fields["email"].widget.attrs.update({"type": "email"})
        self.fields["email"].required = True

        placeholders = {
            "username": "Enter username",
            "email": "Enter email address",
            "password1": "Create a password",
            "password2": "Confirm your password",
        }
        for field, placeholder in placeholders.items():
            self.fields[field].widget.attrs.update({"placeholder": placeholder})


class PartnerFormSubmission(forms.ModelForm):
    class Meta:
        model = PartnerForm
        fields = [
            "name",
            "email",
            "phone_number",
            "preferred_contact",
            "agency_name",
            "agency_website",
            "agency_size",
            "referral_source",
            "additional_info",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "name",
                    "placeholder": "Full Name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "id": "email",
                    "placeholder": "Your Email",
                }
            ),
            "phone_number": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "phone",
                    "placeholder": "Your Phone Number",
                }
            ),
            "preferred_contact": forms.RadioSelect(
                attrs={
                    "class": "form-check-input",
                }
            ),
            "agency_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "agency-name",
                    "placeholder": "Agency Name",
                }
            ),
            "agency_website": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "agency-website",
                    "placeholder": "Agency Website",
                }
            ),
            "agency_size": forms.Select(
                attrs={
                    "class": "form-select",
                    "id": "agency-size",
                }
            ),
            "referral_source": forms.RadioSelect(
                attrs={
                    "class": "form-check-input",
                }
            ),
            "additional_info": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "id": "additional-info",
                    "rows": "5",
                    "placeholder": "Tell us about your business, typical clients, or ask us any questions you might have.",
                }
            ),
        }
        labels = {
            "name": "Full Name",
            "email": "Email",
            "phone_number": "Phone Number",
            "preferred_contact": "Preferred Contact Method",
            "agency_name": "Agency Name",
            "agency_website": "Agency Website",
            "agency_size": "How Many Agents in Your Agency?",
            "referral_source": "How Did You Hear About Us?",
            "additional_info": "Additional Information",
        }


class ContactUsFormSubmission(forms.ModelForm):
    # Honeypot — hidden field that bots fill out, humans never see
    website = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            "tabindex": "-1",
            "autocomplete": "off",
            "style": "position:absolute;left:-9999px;opacity:0;height:0;width:0;",
        }),
        label="",
    )
    # Timestamp to detect instant submissions (bots)
    form_loaded_at = forms.CharField(
        widget=forms.HiddenInput(),
        required=False,
    )

    class Meta:
        model = ContactUsForm
        fields = [
            "first_name",
            "last_name",
            "phone_number",
            "email",
            "contact_method",
            "about",
        ]
        widgets = {
            "first_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "firstName",
                    "placeholder": "First Name",
                    "autocomplete": "given-name",
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "lastName",
                    "placeholder": "Last Name",
                    "autocomplete": "family-name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "id": "email",
                    "placeholder": "Your Email",
                    "autocomplete": "email",
                }
            ),
            "phone_number": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "phone",
                    "placeholder": "Your Phone Number",
                    "autocomplete": "tel",
                }
            ),
            "contact_method": forms.RadioSelect(
                attrs={
                    "class": "form-check-input",
                }
            ),
            "about": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "id": "tripDetails",
                    "rows": "5",
                    "placeholder": "Tell us about your dream destination, travel dates, number of travelers, and any special requirements...",
                }
            ),
        }
        labels = {
            "first_name": "First Name",
            "last_name": "Last Name",
            "email": "Email",
            "phone_number": "Phone Number",
            "contact_method": "",
            "about": "About Your Trip",
        }

    def clean(self):
        cleaned = super().clean()

        # 1. Honeypot — if filled, it's a bot
        if cleaned.get("website"):
            raise ValidationError("Something went wrong. Please try again.")

        # 2. Speed check — submitted in under 3 seconds = bot
        loaded_at = cleaned.get("form_loaded_at", "")
        if loaded_at:
            try:
                elapsed = time.time() - float(loaded_at)
                if elapsed < 3:
                    raise ValidationError("Please wait a moment before submitting.")
            except (ValueError, TypeError):
                pass

        # 3. Content scoring — see users/spam.py for the rules and how each
        #    one was weighted against real submissions. Blocked here means the
        #    row is never written, so no thank-you email and no dispatcher task.
        self.spam_score, self.spam_reasons = score_submission(
            first_name=cleaned.get("first_name", ""),
            last_name=cleaned.get("last_name", ""),
            email=cleaned.get("email", ""),
            phone_number=cleaned.get("phone_number", ""),
            about=cleaned.get("about", ""),
        )
        if self.spam_score >= BLOCK_THRESHOLD:
            # Logged rather than silently dropped: if a rule ever misfires on a
            # real customer, the inquiry is recoverable from the log.
            logger.warning(
                "Blocked contact submission (score %s: %s) from %r %r <%s> %s: %r",
                self.spam_score,
                ", ".join(self.spam_reasons),
                cleaned.get("first_name", ""),
                cleaned.get("last_name", ""),
                cleaned.get("email", ""),
                cleaned.get("phone_number", ""),
                (cleaned.get("about", "") or "")[:300],
            )
            raise ValidationError("Your submission could not be processed.")

        return cleaned
