from decimal import Decimal

from django import forms
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Q

from rates.models import Vehicle

from . import phones, regular_shifts
from .document_uploads import prepare_document_upload, sniff_and_validate
from .models import Driver, DriverWeeklySchedule, FleetVehicle, ShiftTemplate


class _RegularCarField(forms.ModelMultipleChoiceField):
    """Fleet units by number: '#008 · 2022 Chevrolet Suburban', flagged when
    the unit is out of service."""

    def label_from_instance(self, unit):
        label = f"#{unit.vehicle_number} · {unit.year} {unit.make} {unit.model}"
        return label if unit.is_active else f"{label} (inactive)"


class DriverProfileForm(forms.ModelForm):
    """Everyday fields staff need to touch, surfaced on the driver profile
    page so a phone number or a license expiration doesn't require a trip to
    /admin. Deliberately narrower than the full admin form — Gusto payroll
    matching, auto-assign scheduling defaults, and pay rates stay admin-only.

    The Shift facts fields (structured shifts, Stage 1) are the driver's hard
    limits, days a week, extra-shift days and regular car. A limit that a
    confirmed regular day breaks is refused, naming the day (clean()).
    """

    certified_vehicle_types = forms.ModelMultipleChoiceField(
        queryset=Vehicle.objects.filter(requires_certification=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Cleared to drive",
        help_text="Restricted vehicle types this driver is certified for — e.g. the "
                  "Sprinter / 14-pax van, which also requires a current DOT medical card.",
    )
    # Declared here rather than in Meta.widgets: the model field's form field
    # would put min="0" back on the input over the widget's min="1".
    max_days_per_week = forms.IntegerField(
        min_value=1, max_value=7, required=False, label="Days a week",
        widget=forms.NumberInput(attrs={"min": 1, "max": 7}),
    )
    extra_shift_days = forms.TypedMultipleChoiceField(
        coerce=int, choices=DriverWeeklySchedule.DAY_CHOICES, required=False,
        widget=forms.CheckboxSelectMultiple, label="Open to extra shifts on",
    )
    preferred_vehicles = _RegularCarField(
        queryset=FleetVehicle.objects.filter(is_active=True).order_by("vehicle_number"),
        required=False, widget=forms.CheckboxSelectMultiple, label="Regular car",
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
            "hard_earliest_start", "hard_latest_finish", "hard_latest_finish_next_day",
            "max_days_per_week", "extra_shift_days", "preferred_vehicles",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "license_expiration": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "license_date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "chauffeur_permit_expiration": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "dot_medical_card_expiration": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "hired_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "phone_number": forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "placeholder": "407-555-0134"}),
            "hard_earliest_start": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "hard_latest_finish": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }
        # The words dispatcher pages use (the error banner prints these).
        labels = {
            "hard_earliest_start": "Never starts before",
            "hard_latest_finish": "Never finishes after",
            "hard_latest_finish_next_day": "Next day",
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
            # Same for the regular car: a unit taken out of service stays on
            # the list, ticked, for a driver who still has it.
            self.fields["preferred_vehicles"].queryset = FleetVehicle.objects.filter(
                Q(is_active=True) | Q(preferring_drivers=self.instance)
            ).distinct().order_by("vehicle_number")
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

    # The fields a confirmed regular week is judged against (clean()).
    LIMIT_FIELDS = frozenset({"hard_earliest_start", "hard_latest_finish",
                              "hard_latest_finish_next_day", "max_days_per_week"})

    def clean_phone_number(self):
        return clean_phone(self.cleaned_data.get("phone_number"), required=False)

    def clean_extra_shift_days(self):
        return sorted(set(self.cleaned_data.get("extra_shift_days") or []))

    def clean(self):
        cleaned = super().clean()
        # "Next day" means nothing without a latest finish; it is not stored alone.
        if cleaned.get("hard_latest_finish") is None:
            cleaned["hard_latest_finish_next_day"] = False
        # Limits edited later must not break a confirmed regular shift (Review
        # Focus 5): each day they cut, and a days-a-week limit below the
        # working days, is an error on the form, so nothing saves. Judged only
        # when a limit is edited, so a conflict already there (a limit set in
        # admin, say) never blocks saving a phone number or a license date.
        if (self.instance.pk and self.instance.has_regular_shift
                and not self.LIMIT_FIELDS.isdisjoint(self.changed_data)):
            for message in regular_shifts.limit_messages(
                    regular_shifts.current_days(self.instance),
                    hard_earliest_start=cleaned.get("hard_earliest_start"),
                    hard_latest_finish=cleaned.get("hard_latest_finish"),
                    hard_latest_finish_next_day=cleaned.get("hard_latest_finish_next_day",
                                                            False),
                    max_days_per_week=cleaned.get("max_days_per_week")):
                self.add_error(None, message)
        return cleaned

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


# ── Regular shifts (structured shifts, Stage 1) ─────────────────────────────

def _shape_choices(templates):
    """[(id, name)] of the shapes in their order: Morning, Midday, Evening, Float."""
    return [(t.id, t.name) for t in sorted(templates.values(), key=lambda t: (t.sort_order, t.id))]


def _time_field(label):
    return forms.TimeField(
        required=False, label=label,
        widget=forms.TimeInput(attrs={"type": "time", "class": "gt-field"}, format="%H:%M"),
    )


class RegularShiftForm(forms.Form):
    """The Regular Shifts editor's first two steps: the driver's usual shift
    (S15) and the days he works. A day missing from works_on is Off, whatever
    its row in RegularDayFormSet says."""

    role = forms.TypedChoiceField(
        coerce=int, label="Usual shift", widget=forms.RadioSelect,
        error_messages={"required": "Pick the driver's usual shift."},
    )
    works_on = forms.TypedMultipleChoiceField(
        coerce=int, choices=DriverWeeklySchedule.DAY_CHOICES, required=False,
        widget=forms.CheckboxSelectMultiple, label="Works on",
    )

    def __init__(self, *args, templates, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = _shape_choices(templates)

    def clean_works_on(self):
        return sorted(set(self.cleaned_data.get("works_on") or []))


class RegularDayForm(forms.Form):
    """One weekday of the editor (S17): a different shape ("" = same as the
    usual shift), a second shape it may be instead, its times (both blank =
    the shape's usual times) and its own limits."""

    template = forms.TypedChoiceField(coerce=int, empty_value=None, required=False, label="Shift")
    alt_template = forms.TypedChoiceField(coerce=int, empty_value=None, required=False,
                                          label="or also")
    start = _time_field("Leaves base")
    end = _time_field("Back at base")
    day_earliest_start = _time_field("Not before")
    day_latest_finish = _time_field("Done by")
    day_latest_finish_next_day = forms.BooleanField(
        required=False, label="Next day",
        widget=forms.CheckboxInput(attrs={"class": "gt-checkbox"}),
    )

    def __init__(self, *args, shapes=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].choices = [("", "Same as usual"), *shapes]
        self.fields["alt_template"].choices = [("", "—"), *shapes]
        for name in ("template", "alt_template"):
            self.fields[name].widget.attrs["class"] = "gt-field"


def regular_week(week, rows):
    """The editor's two steps put together as seven regular_shifts.DayShift,
    Monday..Sunday: a day not in works_on is Off, "Same as usual" takes the
    usual shift, and blank times stay blank (the shape's usual times, S17).
    ``week`` and each of ``rows`` are cleaned_data; a missing value reads as
    blank, so a half-valid page can still be labelled."""
    role = week.get("role")
    works_on = set(week.get("works_on") or [])
    days = []
    for i, row in enumerate(rows):
        if i not in works_on:
            days.append(regular_shifts.DayShift(i, None, None, None))
            continue
        days.append(regular_shifts.DayShift(
            i, row.get("template") or role, row.get("start"), row.get("end"),
            alt_template_id=row.get("alt_template"),
            day_earliest=row.get("day_earliest_start"),
            day_latest=row.get("day_latest_finish"),
            day_latest_next_day=bool(row.get("day_latest_finish_next_day"))))
    return days


class BaseRegularDayFormSet(forms.BaseFormSet):
    """The editor's seven days, Monday..Sunday. clean() puts them together
    with the usual shift and the working days (``week_form``) via
    regular_week() and refuses the week with every message
    regular_shifts.validate_regular_shift has, in day order. A week that
    passes is left on ``.days`` for the view to save.

    With no usual shift picked (``week_form`` invalid), the days that have a
    shift of their own are still checked, so their problems show in the same
    pass; a "Same as usual" day reads as Off until the usual shift is picked.
    Never more than seven forms (absolute_max): a page that sends more is
    refused with Django's own "at most 7" message."""

    def __init__(self, *args, week_form, driver, templates, rest_min, **kwargs):
        self.week_form, self.driver = week_form, driver
        self.templates, self.rest_min = templates, rest_min
        self.days = None
        kwargs.setdefault("prefix", "days")
        kwargs["form_kwargs"] = {**kwargs.get("form_kwargs", {}),
                                 "shapes": _shape_choices(templates)}
        super().__init__(*args, **kwargs)

    def clean(self):
        if any(self.errors):
            return
        if self.total_form_count() != 7:
            raise forms.ValidationError("The page lost a day — reload it and try again.")
        week_ok = self.week_form.is_valid()
        # An invalid week form keeps the fields that did clean: works_on, and
        # no role, so "Same as usual" days come out Off and are not checked.
        week = regular_week(self.week_form.cleaned_data, [f.cleaned_data for f in self.forms])
        problems = regular_shifts.validate_regular_shift(
            week, templates=self.templates,
            hard_earliest_start=self.driver.hard_earliest_start,
            hard_latest_finish=self.driver.hard_latest_finish,
            hard_latest_finish_next_day=self.driver.hard_latest_finish_next_day,
            max_days_per_week=self.driver.max_days_per_week,
            rest_min=self.rest_min)
        if problems:
            raise forms.ValidationError(problems)
        if week_ok:
            self.days = week


RegularDayFormSet = forms.formset_factory(
    RegularDayForm, formset=BaseRegularDayFormSet, extra=0,
    max_num=7, validate_max=True, absolute_max=7)


def _join_days(days) -> str:
    """[0, 1, 4] -> 'Monday, Tuesday and Friday'."""
    names = [regular_shifts.DAY_NAMES[d] for d in days]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _hours(minutes) -> Decimal:
    """720 -> 12, 690 -> 11.5: the longest shift as the page shows it. A value
    set in admin that isn't a tenth of an hour is rounded to one."""
    hours = Decimal(minutes) / 60
    tenth = hours.quantize(Decimal("0.1"))
    return hours if hours == tenth else tenth


class ShiftTemplateForm(forms.ModelForm):
    """One shape on the Shift Templates page (Task 8): its usual leave and
    return times (targets, not limits — U11), its notes, and the longest shift
    it may run, in hours. The hours are written to max_span_minutes in clean();
    no more than 12 (720 minutes, the ceiling every regular day is held to),
    and never below a confirmed regular day on this shape — those have to be
    edited first."""

    # step_size is a Decimal: with a float, Django's step check can't word its
    # own error for a value like 11.25 (Decimal + float).
    max_span_hours = forms.DecimalField(
        min_value=1, max_value=12, decimal_places=1, step_size=Decimal("0.5"),
        label="Longest shift (hours)",
        error_messages={"max_value": "No shift can be longer than 12 hours.",
                        "min_value": "A shift must be at least 1 hour.",
                        "step_size": "Use whole or half hours, like 11 or 11.5."},
    )

    class Meta:
        model = ShiftTemplate
        # max_span_minutes is deliberately left out: the page works in hours.
        fields = ["start_earliest", "start_latest", "end_earliest", "end_latest", "notes"]
        widgets = {
            **{name: forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
               for name in ("start_earliest", "start_latest", "end_earliest", "end_latest")},
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "start_earliest": "Leaves base, earliest",
            "start_latest": "Leaves base, latest",
            "end_earliest": "Back at base, earliest",
            "end_latest": "Back at base, latest",
            "notes": "Notes",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("max_span_hours", _hours(self.instance.max_span_minutes))
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "gt-field")
            # The page prints no caption under a field (the model's help_text is
            # written for the admin), so no aria-describedby to a missing id.
            field.help_text = ""

    def clean(self):
        cleaned = super().clean()
        hours = cleaned.get("max_span_hours")
        if hours is None:
            return cleaned
        minutes = int(hours * 60)
        self.instance.max_span_minutes = minutes
        # Judged only when the hours change, so a day made longer in admin
        # never blocks saving this shape's times or notes.
        if self.instance.pk and "max_span_hours" in self.changed_data:
            longer = regular_shifts.confirmed_days_longer_than(self.instance, minutes)
            if longer:
                n, name = len(longer), self.instance.name
                first = (f"1 confirmed regular shift on {name} is longer than that — "
                         "edit it first." if n == 1 else
                         f"{n} confirmed regular shifts on {name} are longer than that — "
                         "edit them first.")
                by_driver = {}
                for driver, day in longer:
                    by_driver.setdefault(driver, []).append(day)
                who = "; ".join(f"{driver} on {_join_days(days)}"
                                for driver, days in by_driver.items())
                self.add_error("max_span_hours",
                               [first, f"Longer than {minutes / 60:g} hours: {who}."])
        return cleaned


# The shapes are edited in place: the page never adds one (edit_only), and a
# shape can't be deleted (drivers' regular days point at it).
ShiftTemplateFormSet = forms.modelformset_factory(
    ShiftTemplate, form=ShiftTemplateForm, extra=0, can_delete=False, edit_only=True)


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
