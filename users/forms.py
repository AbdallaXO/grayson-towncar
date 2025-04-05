from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django import forms
from .models import PartnerForm


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
            "referral_source": "How Did You Hear About Us?",
            "additional_info": "Additional Information",
        }
