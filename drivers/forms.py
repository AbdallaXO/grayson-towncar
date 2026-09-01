from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Q

from rates.models import Vehicle

from .document_uploads import prepare_document_upload
from .models import Driver


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
