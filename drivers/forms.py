from django import forms
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Q
from django.utils import timezone

from rates.models import Vehicle
from reservations.models import Leg

from . import phones
from .document_uploads import prepare_document_upload, sniff_and_validate
from .driver_knowledge import recent_legs_for, trip_label
from .models import Driver, DriverLogEntry


class DriverProfileForm(forms.ModelForm):
    """Everyday fields staff need to touch, surfaced on the driver profile
    page so a phone number or a license expiration doesn't require a trip to
    /admin. Deliberately narrower than the full admin form — Gusto payroll
    matching, auto-assign scheduling defaults, and pay rates stay admin-only.
    """

    certified_vehicle_types = forms.ModelMultipleChoiceField(
        queryset=Vehicle.objects.filter(requires_certification=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Cleared to drive",
        help_text="Restricted vehicle types this driver is certified for — e.g. the "
                  "Sprinter / 14-pax van, which also requires a current DOT medical card.",
    )

    class Meta:
        model = Driver
        fields = [
            "phone_number", "vehicle", "payment_method", "night_bonus",
            "employment_type", "is_active", "notes",
            "hired_on", "home_address",
            "license_number", "license_state", "license_class",
            "license_expiration", "license_scan",
            "license_full_name", "license_date_of_birth", "license_address",
            "chauffeur_permit_number", "chauffeur_permit_fdl_number",
            "chauffeur_permit_expiration", "chauffeur_permit_scan",
            "dot_medical_card_expiration", "dot_medical_card_scan",
            "certified_vehicle_types",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "license_expiration": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "license_date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "chauffeur_permit_expiration": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "dot_medical_card_expiration": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "hired_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "phone_number": forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "placeholder": "407-555-0134"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Widen the choices to include anything this driver is ALREADY
        # certified for, even a vehicle no longer flagged
        # requires_certification=True (e.g. ops toggles it off, or admin's
        # unrestricted filter_horizontal certified them for an ordinary
        # vehicle). Without this, saving the form for ANY field — not just
        # this one — calls Vehicle M2M .set() against only the flagged
        # subset, silently dropping a certification the checkbox list never
        # even offered to uncheck.
        if self.instance.pk:
            self.fields["certified_vehicle_types"].queryset = Vehicle.objects.filter(
                Q(requires_certification=True) | Q(certified_drivers=self.instance)
            ).distinct()
        # Every visible-typed input gets one shared CSS hook so the template
        # doesn't have to repeat widget attrs field by field. Checkbox lists
        # (certified_vehicle_types) are left alone — their <input>s are styled
        # through the wrapping container instead.
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "gt-checkbox")
            elif isinstance(widget, forms.CheckboxSelectMultiple):
                continue
            elif isinstance(widget, forms.ClearableFileInput):
                widget.attrs.setdefault("class", "gt-file")
            else:
                widget.attrs.setdefault("class", "gt-field")
        # driver_profile.html deliberately keeps this form compact — a bare
        # label per field, no caption row — unlike the model's help_text,
        # which is written for the Django admin (which DOES render a caption
        # under every field). Since Django 5 auto-adds
        # aria-describedby="..._helptext" for any field with help_text
        # whether or not the template renders that caption, leaving the
        # model's help_text on these form fields means every one of them
        # points at an id that's never in the DOM. Clear it here, on the
        # FORM field only — the model field (and the admin) keep it.
        for field in self.fields.values():
            field.help_text = ""

    def clean_phone_number(self):
        return clean_phone(self.cleaned_data.get("phone_number"), required=False)

    def _clean_scan(self, field_name):
        """Route a newly-uploaded scan through the same content-sniffing /
        compressing / renaming gate document_uploads.py enforces for driver
        self-service uploads (see that module's docstring) — a staff
        FileField with no clean_<field> override would otherwise write the
        client-declared Content-Type and original filename straight to the
        public media bucket. Only new uploads (UploadedFile) need this; an
        unchanged or cleared existing FieldFile passes through untouched.
        """
        upload = self.cleaned_data.get(field_name)
        if not isinstance(upload, UploadedFile):
            return upload
        prepared, error = prepare_document_upload(upload)
        if error:
            raise forms.ValidationError(error)
        return prepared

    def clean_license_scan(self):
        return self._clean_scan("license_scan")

    def clean_chauffeur_permit_scan(self):
        return self._clean_scan("chauffeur_permit_scan")

    def clean_dot_medical_card_scan(self):
        return self._clean_scan("dot_medical_card_scan")


class DriverLicenseDetailsForm(forms.ModelForm):
    """Driver self-service: the license fields, shown pre-filled after a scan
    (see drivers.license_ocr) or filled by hand. Deliberately excludes the
    scan file itself (the view saves that directly, independent of whether
    these details validate) and everything else on Driver — the permit and
    DOT medical card stay photo-only self-service, and pay/contact/notes stay
    on the staff-only DriverProfileForm.

    Includes name/DOB/address read off the license, kept alongside (not
    merged into) the account's own profile.first_name/last_name — they're for
    matching against the account, not replacing it, and commonly do differ
    (maiden name, a nickname on the account, a stale address).

    Uses the driver portal's own gt-input styling (drivers/_driver_head.html),
    not the staff dark-theme classes DriverProfileForm applies.
    """

    class Meta:
        model = Driver
        fields = [
            "license_number", "license_state", "license_class", "license_expiration",
            "license_full_name", "license_date_of_birth", "license_address",
        ]
        widgets = {
            "license_number": forms.TextInput(attrs={"class": "gt-input"}),
            "license_state": forms.TextInput(attrs={"class": "gt-input", "placeholder": "e.g. FL"}),
            "license_class": forms.TextInput(attrs={"class": "gt-input", "placeholder": "e.g. E"}),
            "license_expiration": forms.DateInput(
                attrs={"class": "gt-input", "type": "date"}, format="%Y-%m-%d"
            ),
            "license_full_name": forms.TextInput(attrs={"class": "gt-input"}),
            "license_date_of_birth": forms.DateInput(
                attrs={"class": "gt-input", "type": "date"}, format="%Y-%m-%d"
            ),
            "license_address": forms.TextInput(attrs={"class": "gt-input"}),
        }


class DriverPermitDetailsForm(forms.ModelForm):
    """Driver self-service: the chauffeur-permit fields, shown pre-filled
    after a scan (see drivers.permit_ocr) or filled by hand. Same contract as
    DriverLicenseDetailsForm above — excludes the scan file itself (the view
    saves that directly, independent of whether these details validate), and
    the DOT medical card stays photo-only self-service.

    The FDL# saved here is cross-checked against license_number by
    Driver.chauffeur_permit_fdl_mismatch, which is why it's collected at all.
    """

    class Meta:
        model = Driver
        fields = [
            "chauffeur_permit_number", "chauffeur_permit_fdl_number",
            "chauffeur_permit_expiration",
        ]
        widgets = {
            "chauffeur_permit_number": forms.TextInput(attrs={"class": "gt-input"}),
            "chauffeur_permit_fdl_number": forms.TextInput(attrs={"class": "gt-input"}),
            "chauffeur_permit_expiration": forms.DateInput(
                attrs={"class": "gt-input", "type": "date"}, format="%Y-%m-%d"
            ),
        }
        # The model help_texts are written for staff (they reference other
        # fields by name); these are what a driver sees on the confirm step.
        help_texts = {
            "chauffeur_permit_number": "PERMIT# on the card.",
            "chauffeur_permit_fdl_number": "FDL# on the card — the driver's-license "
                                           "number printed on the permit.",
        }


# ── Onboarding ──────────────────────────────────────────────────────────────

def clean_phone(raw, *, required):
    """Shared validator: blank is fine unless required; anything else must parse
    to E.164 (drivers/phones.py) and is returned in that form."""
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise forms.ValidationError("A mobile number is required.")
        return ""
    normalized = phones.normalize(raw)
    if not normalized:
        raise forms.ValidationError(phones.INVALID_MESSAGE)
    return normalized


_username_validator = RegexValidator(
    r"^[a-z0-9._-]{3,30}$",
    "3–30 characters: lowercase letters, numbers, dots, dashes or underscores.",
)


class NewDriverForm(forms.Form):
    """Staff side of "Add a driver": the handful of facts needed to create the
    account and send the welcome link. Everything else is filled in later — by
    the driver on My details / My documents, or by staff on the profile."""

    SEND_CHOICES = [
        ("sms", "Text the link to their mobile"),
        ("email", "Email the link"),
        ("link", "Just give me the link to send myself"),
    ]

    first_name = forms.CharField(max_length=60)
    last_name = forms.CharField(max_length=60, required=False)
    phone_number = forms.CharField(
        max_length=25, required=False,
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "off", "placeholder": "407-555-0134"}),
    )
    email = forms.EmailField(required=False, widget=forms.EmailInput(attrs={"autocomplete": "off"}))
    driver_type = forms.ChoiceField(choices=Driver.DRIVER_TYPE_CHOICES, initial="inhouse")
    portal_role = forms.ChoiceField(choices=Driver.PORTAL_ROLE_CHOICES, initial="driver")
    employment_type = forms.ChoiceField(
        choices=[("", "Not sure yet")] + Driver.EMPLOYMENT_TYPE_CHOICES, required=False,
    )
    hired_on = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    send_via = forms.ChoiceField(choices=SEND_CHOICES, initial="sms", widget=forms.RadioSelect)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "send_via":
                continue
            field.widget.attrs.setdefault("class", "gt-field")

    def clean_phone_number(self):
        number = clean_phone(self.cleaned_data.get("phone_number"), required=False)
        if number:
            twin = Driver.objects.filter(phone_number=number).select_related("profile").first()
            if twin:
                raise forms.ValidationError(
                    f"{twin} already has this number. Open their profile to send a welcome link instead."
                )
        return number

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Someone already has an account with this email address.")
        return email

    def clean(self):
        data = super().clean()
        via = data.get("send_via")
        if via == "sms" and not data.get("phone_number") and "phone_number" not in self.errors:
            self.add_error("phone_number", "Enter their mobile number to text the link.")
        if via == "email" and not data.get("email") and "email" not in self.errors:
            self.add_error("email", "Enter their email address to email the link.")
        return data


class DriverWelcomeForm(SetPasswordForm):
    """What a new chauffeur fills in when they open their welcome link: a
    username they will remember, and a password (twice)."""

    username = forms.CharField(
        max_length=30, validators=[_username_validator],
        widget=forms.TextInput(attrs={"autocomplete": "username", "autocapitalize": "none", "spellcheck": "false"}),
    )
    # Asked for while they have the phone in hand, never a reason the login
    # fails. Deliberately not labelled "optional" on the page.
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autocapitalize": "none", "spellcheck": "false"}),
    )
    # Optional: a photo of the license while they have the phone in hand. Not a
    # model field — the view stores it through the same read-and-confirm step
    # as My Documents. Never required here: the login must not fail because a
    # photo did.
    license_scan = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={
            "accept": "image/jpeg,image/png,image/heic,image/heif,application/pdf",
            "class": "a-file-input",
        }),
    )

    field_order = ["username", "email", "new_password1", "new_password2", "license_scan"]

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["username"].initial = user.username
        self.fields["email"].initial = user.email
        self.fields["new_password1"].widget.attrs.update({"autocomplete": "new-password"})
        self.fields["new_password2"].widget.attrs.update({"autocomplete": "new-password"})
        for f in self.fields.values():
            f.help_text = ""

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip().lower()
        clash = User.objects.filter(username__iexact=username).exclude(pk=self.user.pk)
        if clash.exists():
            raise forms.ValidationError("That username is taken — try adding a number.")
        return username

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email and User.objects.filter(email__iexact=email).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError("Another account already uses this email address.")
        return email

    def clean_license_scan(self):
        upload = self.cleaned_data.get("license_scan")
        if not upload:
            return None
        _, error = sniff_and_validate(upload)
        if error:
            raise forms.ValidationError(error)
        return upload


class DriverMyDetailsForm(forms.ModelForm):
    """The chauffeur's own view of their contact details, in the driver app.
    Name and email live on the User row; the rest on Driver."""

    first_name = forms.CharField(max_length=60)
    last_name = forms.CharField(max_length=60, required=False)
    email = forms.EmailField(required=False)
    # Not a model field on purpose: the photo is stored by the view through
    # the same scan-and-confirm step My Documents uses, not by form.save().
    license_scan = forms.FileField(
        required=False, label="Driver's license photo",
        widget=forms.FileInput(attrs={
            "accept": "image/jpeg,image/png,image/heic,image/heif,application/pdf",
            "class": "scan-input",
        }),
    )

    class Meta:
        model = Driver
        fields = ["phone_number", "home_address"]
        widgets = {
            "phone_number": forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "placeholder": "407-555-0134"}),
            "home_address": forms.TextInput(attrs={"autocomplete": "street-address", "placeholder": "Street, city, state, ZIP"}),
        }

    def __init__(self, *args, require_license=False, onboarding=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_license = require_license
        # The welcome flow is the one time the driver has the phone in hand and
        # is expecting to fill things in, so it insists on last name and home
        # address. Email and the license photo are asked for but never block:
        # a driver must be able to finish without them. Ordinary edits stay lenient.
        self.onboarding = onboarding
        user = self.instance.profile
        self.fields["first_name"].initial = user.first_name
        self.fields["last_name"].initial = user.last_name
        self.fields["email"].initial = user.email
        if onboarding:
            self.fields["last_name"].required = True
            self.fields["last_name"].error_messages["required"] = "Please add your last name."
            self.fields["home_address"].required = True
            self.fields["home_address"].error_messages["required"] = (
                "Please add your home address — the office needs it on file."
            )
        for f in self.fields.values():
            f.widget.attrs.setdefault("class", "gt-input")
            f.help_text = ""

    def clean_license_scan(self):
        upload = self.cleaned_data.get("license_scan")
        if not upload:
            if self.require_license:
                raise forms.ValidationError(
                    "Please add a photo of your driver's license — the office needs it on file before your first trip."
                )
            return None
        _, error = sniff_and_validate(upload)
        if error:
            raise forms.ValidationError(error)
        return upload

    def clean_phone_number(self):
        return clean_phone(self.cleaned_data.get("phone_number"), required=True)

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email and User.objects.filter(email__iexact=email).exclude(pk=self.instance.profile.pk).exists():
            raise forms.ValidationError("Another account already uses this email address.")
        return email

    def save(self, commit=True):
        from django.utils import timezone

        driver = super().save(commit=False)
        driver.details_confirmed_at = timezone.now()
        user = driver.profile
        user.first_name = self.cleaned_data["first_name"].strip()
        user.last_name = (self.cleaned_data.get("last_name") or "").strip()
        user.email = self.cleaned_data.get("email") or ""
        if commit:
            user.save(update_fields=["first_name", "last_name", "email"])
            driver.save()
        return driver


class _TripChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, leg):
        return trip_label(leg)


class DriverLogEntryForm(forms.ModelForm):
    """One entry in a driver's log, on the staff profile (structured shifts,
    Stage 1, S19). Staff-only.

    The trip picker offers this driver's trips from the last 60 days (plus the
    entry's own trip when editing). Only a manager sees the strike box: for
    anyone else it is not on the form at all, so posting it does nothing.
    A severity is kept only on a complaint or an incident.
    """
    FUTURE_DATE = "That date is in the future."
    STRIKE_KIND = "A strike must be a complaint or an incident."

    leg = _TripChoiceField(queryset=None, required=False, label="Trip (optional)",
                           empty_label="No trip")

    class Meta:
        model = DriverLogEntry
        fields = ["kind", "occurred_on", "severity", "leg", "summary", "details", "is_strike"]
        labels = {
            "kind": "What kind",
            "occurred_on": "When it happened",
            "severity": "How serious",
            "summary": "What happened",
            "details": "Details (optional)",
            "is_strike": "Mark as a strike",
        }
        widgets = {
            "occurred_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "summary": forms.TextInput(attrs={"placeholder": "One line, e.g. Guest wrote in to thank him"}),
            "details": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, driver, user, *args, **kwargs):
        kwargs.setdefault("auto_id", "log_%s")      # ids apart from the profile edit form's
        super().__init__(*args, **kwargs)
        self.driver = driver
        self.today = today = timezone.localdate()
        if self.instance.pk is None:
            self.instance.driver = driver
            self.fields["occurred_on"].initial = today
        self.fields["occurred_on"].widget.attrs["max"] = today.isoformat()

        legs = recent_legs_for(driver, today)
        if self.instance.leg_id:            # an older trip stays on its own entry
            legs = (Leg.objects.filter(Q(pk__in=legs.values("pk")) | Q(pk=self.instance.leg_id))
                    .order_by("-pickup_date", "-pickup_time", "-id"))
        self.fields["leg"].queryset = legs

        # A kind must be picked: no silent default between a compliment and a complaint.
        self.fields["kind"].choices = [("", "Pick one")] + list(self.fields["kind"].choices)[1:]
        if not user.is_superuser:
            del self.fields["is_strike"]

        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "gt-checkbox")
            else:
                field.widget.attrs.setdefault("class", "gt-field")
            field.help_text = ""

    def clean(self):
        cleaned = super().clean()
        occurred_on = cleaned.get("occurred_on")
        if occurred_on and occurred_on > self.today:
            self.add_error("occurred_on", self.FUTURE_DATE)
        kind = cleaned.get("kind")
        if cleaned.get("is_strike") and kind not in DriverLogEntry.STRIKE_KINDS:
            self.add_error("is_strike", self.STRIKE_KIND)
        if kind and kind not in DriverLogEntry.STRIKE_KINDS:
            cleaned["severity"] = ""        # a compliment or a note has no severity
        return cleaned
