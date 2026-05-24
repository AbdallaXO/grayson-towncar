import re
import time

from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django import forms
from django.core.exceptions import ValidationError
from .models import PartnerForm, ContactUsForm


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

    # Cyrillic / spam patterns
    CYRILLIC_RE = re.compile(r'[\u0400-\u04FF]')
    URL_RE = re.compile(r'https?://', re.IGNORECASE)
    SPAM_KEYWORDS = [
        'tinyurl.com', 'bit.ly', 'руб', 'перевод', 'сюрприз',
        'подарок', 'новости', 'ссылк',
    ]

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

        # 3. Cyrillic / spam content check
        text_fields = [
            cleaned.get("first_name", ""),
            cleaned.get("last_name", ""),
            cleaned.get("about", ""),
        ]
        combined = " ".join(text_fields).lower()

        if self.CYRILLIC_RE.search(combined):
            raise ValidationError("Your submission could not be processed.")

        # URLs in name fields = spam
        for field in ["first_name", "last_name"]:
            if self.URL_RE.search(cleaned.get(field, "")):
                raise ValidationError("Your submission could not be processed.")

        # Known spam keywords
        if any(kw in combined for kw in self.SPAM_KEYWORDS):
            raise ValidationError("Your submission could not be processed.")

        return cleaned
