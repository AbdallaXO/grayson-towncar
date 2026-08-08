from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from reservations.models import Leg
from decimal import Decimal
from datetime import timedelta


class Driver(models.Model):
    DRIVER_TYPE_CHOICES = [
        ("inhouse", "Inhouse"),
        ("affiliate", "Affiliate/Outhouse"),
    ]

    # Employment commitment. This is NOT cosmetic: it decides what an available day
    # *means*, and therefore what every load/utilisation metric means.
    #   full_time  -> an available day is a COMMITMENT. A day available but not worked
    #                 is a finding (they expected work and got none).
    #   part_time  -> an available day is an OFFER. Not working it is normal.
    # Deliberately blank by default — guessing inverts the meaning of the metrics, so an
    # unlabelled driver is reported as "unlabelled" rather than silently assumed.
    EMPLOYMENT_TYPE_CHOICES = [
        ("full_time", "Full time — works every available day"),
        ("part_time", "Part time — available days are offers, not commitments"),
    ]


    profile = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=25, null=True, blank=True, help_text="Driver's phone number")
    vehicle = models.CharField(null=True, blank=True, max_length=55)
    schedule = models.CharField(max_length=255, null=True, blank=True, help_text="Driver's availability schedule (e.g., 'Mon-Thu: 4AM-4PM, Fri: 6PM-8PM, Sat-Sun: 5AM-5PM')")
    default_start_hour = models.IntegerField(
        default=6,
        help_text="Default earliest hour this driver is available (0-23). Used as default in auto-assign."
    )
    default_end_hour = models.IntegerField(
        default=23,
        help_text="Default latest hour this driver works until (0-23). Used as default in auto-assign."
    )
    default_flexible = models.BooleanField(
        default=True,
        help_text="Flexible = no hard time limits, planner builds a reasonable shift. Uncheck only if driver has strict start/end constraints."
    )
    default_shift_type = models.CharField(
        max_length=20, default="full_day",
        help_text="Default shift type for days without a weekly override."
    )
    default_max_hours = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True,
        help_text="Default max hours per day. NULL = no limit."
    )
    default_preferred_shift = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Default preferred time of day (morning, evening, etc.)."
    )
    default_preference = models.CharField(
        max_length=30, blank=True, default="",
        help_text="Default trip type preference for auto-assign (e.g., prefer_arrival, only_return)."
    )
    notes = models.TextField(null=True, blank=True, help_text="Internal notes about this driver for dispatchers")
    payment_method = models.CharField(
        max_length=50, default="direct deposit", blank=True
    )
    driver_type = models.CharField(
        max_length=20,
        choices=DRIVER_TYPE_CHOICES,
        default="affiliate",
        help_text="Inhouse drivers work for the company and can drive any vehicle. Affiliates are contractors with specific vehicles."
    )
    employment_type = models.CharField(
        max_length=20,
        choices=EMPLOYMENT_TYPE_CHOICES,
        blank=True,
        default="",
        help_text="Full time means they are expected to work every day they are marked "
                  "available, so an unworked available day is a real gap. Part time means "
                  "available days are offers they need not take. Leave blank if unsure — "
                  "the load reports show unlabelled drivers separately rather than guessing, "
                  "because the wrong label inverts what every number means.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to hide this driver from the directory and assignment pickers. Keeps historical legs/payments intact — use this for drivers who no longer work with us or are on extended leave."
    )
    exclude_from_timing = models.BooleanField(
        default=False,
        help_text="Exclude this driver's completed trips from route timing data. Affiliates are always excluded."
    )
    night_bonus = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("10.00"),
        help_text="Night pickup bonus (10 PM - 6 AM). Set per driver. $0 for no bonus."
    )

    # ── Vehicle capability + preferences (driver knowledge for dispatchers) ──
    # Capability is exception-based: a vehicle TYPE is only restricted if its
    # rates.Vehicle.requires_certification is set (today only the Sprinter / 14-pax).
    # certified_vehicle_types lists the restricted types this driver is cleared for.
    certified_vehicle_types = models.ManyToManyField(
        "rates.Vehicle", blank=True, related_name="certified_drivers",
        help_text="Restricted vehicle types this driver is cleared to drive (e.g. the Sprinter / "
                  "14-pax van). Non-restricted types need no entry here.",
    )
    preferred_vehicle_types = models.ManyToManyField(
        "rates.Vehicle", blank=True, related_name="preferring_drivers",
        help_text="Vehicle type(s) this driver prefers to drive (soft preference — informational).",
    )
    preferred_vehicles = models.ManyToManyField(
        "FleetVehicle", blank=True, related_name="preferring_drivers",
        help_text="Specific vehicle unit(s) this driver prefers / usually drives, e.g. their "
                  "regular car (soft preference — informational).",
    )

    # ── Gusto contractor matching (used only by the Gusto Smart Import CSV export) ──
    # All fields optional. Leave blank to fall back to profile.first_name / last_name
    # for matching. Only the masked last-4 or contractor ID is stored — never the
    # full SSN/EIN. Gusto's Smart Import accepts a masked identifier such as "*9579".
    gusto_first_name = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Legal first name on file with Gusto. Leave blank to use profile first name."
    )
    gusto_last_name = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Legal last name on file with Gusto. Leave blank to use profile last name."
    )
    gusto_business_name = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Business / DBA name if contractor is paid as a business entity."
    )
    gusto_ssn_ein_last4 = models.CharField(
        max_length=5, blank=True, default="",
        help_text="Last 4 digits of SSN or EIN, e.g. \"9579\" or \"*9579\". Used for Gusto matching only. Do NOT enter the full number."
    )
    gusto_contractor_id = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Gusto's internal contractor ID, if known. Optional alternative to last-4."
    )
    GUSTO_PAYMENT_TYPE_CHOICES = [
        ("", "—"),
        ("individual", "Individual (SSN)"),
        ("business", "Business (EIN)"),
    ]
    gusto_payment_type = models.CharField(
        max_length=20, choices=GUSTO_PAYMENT_TYPE_CHOICES, blank=True, default="",
        help_text="How this contractor is paid in Gusto. Informational only."
    )

    def get_unpaid_legs(self):
        """Return all legs that are unpaid regardless of status"""
        return self.legs.filter(payment_status="unpaid")

    def get_total_unpaid_amount(self):
        """Calculate total unpaid amount for this driver using a single DB aggregate."""
        from django.db.models import Case, When, Sum, Value, F
        from django.db.models.functions import Coalesce

        result = self.get_unpaid_legs().aggregate(
            total=Sum(
                Case(
                    When(
                        # New-style: any of base/gratuity/additional is set
                        Q(driver_base_pay__isnull=False)
                        | Q(driver_gratuity__isnull=False)
                        | Q(driver_additional__isnull=False),
                        then=(
                            Coalesce(F("driver_base_pay"), Value(Decimal("0.00")))
                            + Coalesce(F("driver_gratuity"), Value(Decimal("0.00")))
                            + Coalesce(F("driver_additional"), Value(Decimal("0.00")))
                        ),
                    ),
                    # Legacy fallback
                    default=Coalesce(F("driver_pay_amount"), Value(Decimal("0.00"))),
                )
            )
        )
        return result["total"] or Decimal("0.00")

    def get_leg_history(self, start_date=None, end_date=None):
        """
        Get driver's leg history with optional date filtering
        """
        legs = self.legs.all()

        if start_date:
            legs = legs.filter(pickup_date__gte=start_date)
        if end_date:
            legs = legs.filter(pickup_date__lte=end_date)

        return legs.order_by("-pickup_date", "-pickup_time")
    
    def get_phone_number(self):
        """Get driver's phone number"""
        return self.phone_number or "N/A"
    
    def get_upcoming_legs(self, days=7):
        """Get upcoming legs for the next N days"""
        from django.utils import timezone
        from datetime import timedelta
        
        today = timezone.localdate()
        end_date = today + timedelta(days=days)
        
        return self.legs.filter(
            pickup_date__gte=today,
            pickup_date__lte=end_date,
            status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"]
        ).order_by("pickup_date", "pickup_time")
    
    def is_available_today(self):
        """Check if driver has any scheduled trips today"""
        from django.utils import timezone
        
        today = timezone.localdate()
        return self.legs.filter(
            pickup_date=today,
            status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"]
        ).exists()
    
    def get_vehicle_display(self):
        """Get vehicle display - 'Any' for inhouse, specific vehicle for affiliates"""
        if self.driver_type == "inhouse":
            return "Any (Inhouse)"
        return self.vehicle or "Not specified"
    
    def get_schedule_display(self):
        """Format schedule for display in multi-line format"""
        if not self.schedule:
            return None

        # Split by comma and format each part on a new line
        # This handles formats like "Mon-Thu: 4AM-4PM, Fri: 6PM-8PM, Sat-Sun: 5AM-5PM"
        schedule_parts = [part.strip() for part in self.schedule.split(',')]
        return '\n'.join(schedule_parts)

    def get_effective_availability(self, target_date):
        """
        Single source of truth: combine recurring (weekly/default) availability with
        any active DriverDateOverride for `target_date`. Returns a rich dict — see
        drivers.availability.resolve_effective_availability.
        """
        from drivers.availability import resolve_effective_availability
        return resolve_effective_availability(self, target_date)

    def get_availability_for_date(self, target_date):
        """
        Legacy 5-tuple shim: (is_available, start_hour, end_hour, preference, flexible).
        Kept for the auto-assigner and other older callers.
        """
        eff = self.get_effective_availability(target_date)
        return (
            eff["is_available"],
            eff["start_hour"],
            eff["end_hour"],
            eff["preference"],
            eff["flexible"],
        )

    def get_full_availability(self, target_date):
        """
        Dict with shift_type, max_hours, scheduling_notes, plus the new
        effective-availability fields (status, display_label, exception, ...).
        """
        return self.get_effective_availability(target_date)

    # ── Vehicle capability + preference helpers ──────────────────────────────
    def can_drive(self, vehicle_type):
        """True if this driver may be assigned the given rates.Vehicle (type).

        Non-restricted types are drivable by everyone; a restricted type
        (requires_certification) is drivable only if the driver is certified for it.
        `vehicle_type` may be None (unknown type → allowed)."""
        if vehicle_type is None or not getattr(vehicle_type, "requires_certification", False):
            return True
        return self.certified_vehicle_types.filter(pk=vehicle_type.pk).exists()

    def cert_labels(self):
        """Display labels for the restricted vehicle types this driver is cleared for,
        e.g. ['Sprinter']. 'Van(14 Pax)' is shown as 'Sprinter'."""
        labels = []
        for v in self.certified_vehicle_types.all():
            if getattr(v, "requires_certification", False):
                labels.append("Sprinter" if v.vehicle_type == "Van(14 Pax)" else str(v))
        return labels

    def preferred_vehicle_label(self):
        """Combined soft vehicle preference: type(s) and/or specific unit(s),
        e.g. 'SUV · #008'. Empty string when none set."""
        parts = [str(v) for v in self.preferred_vehicle_types.all()]
        parts += [f"#{fv.vehicle_number}" for fv in self.preferred_vehicles.all()]
        return " · ".join(parts)

    def __str__(self):
        if self.profile.first_name:
            return f"{self.profile.first_name} {self.profile.last_name}"
        return self.profile.username


class DriverWeeklySchedule(models.Model):
    """Per-day-of-week availability for a driver. Overrides driver's default_* fields."""
    DAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]
    SHIFT_TYPE_CHOICES = [
        ("morning",  "Morning"),
        ("midday",   "Midday"),
        ("evening",  "Evening"),
        ("night",    "Night"),
        ("split",    "Split AM/PM"),
        ("full_day", "Full Day"),
        ("custom",   "Custom"),
    ]
    PREFERRED_SHIFT_CHOICES = [
        ("", "No Preference"),
        ("morning",  "Prefer Morning"),
        ("midday",   "Prefer Midday"),
        ("evening",  "Prefer Evening"),
        ("night",    "Prefer Night"),
    ]
    PREFERENCE_CHOICES = [
        ("", "Any"),
        ("prefer_arrival", "Prefer Arrivals"),
        ("prefer_return", "Prefer Returns"),
        ("prefer_cruise", "Prefer Cruises"),
        ("heavy_arrival", "Heavy Arrivals"),
        ("heavy_return", "Heavy Returns"),
        ("heavy_cruise", "Heavy Cruises"),
        ("only_arrival", "Arrivals Only"),
        ("only_return", "Returns Only"),
        ("only_cruise", "Cruises Only"),
    ]

    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="weekly_schedule")
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    is_available = models.BooleanField(default=True)
    shift_type = models.CharField(
        max_length=20, choices=SHIFT_TYPE_CHOICES, default="full_day",
        help_text="Shift classification for dispatchers. Named types auto-set hours and flexibility."
    )
    start_hour = models.IntegerField(default=6)
    end_hour = models.IntegerField(default=23)
    flexible = models.BooleanField(
        default=True,
        help_text="Flexible = no hard time limits, planner builds a reasonable shift. Uncheck only if driver has strict start/end constraints."
    )
    max_hours = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True,
        help_text="Maximum hours to schedule this driver per day. NULL = no limit."
    )
    preferred_shift = models.CharField(
        max_length=20, blank=True, default="", choices=PREFERRED_SHIFT_CHOICES,
        help_text="Preferred time of day. Scheduler tries this window first but can assign outside it."
    )
    preference = models.CharField(max_length=30, blank=True, default="", choices=PREFERENCE_CHOICES)
    scheduling_notes = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Short note visible to dispatchers on the schedule board."
    )

    class Meta:
        unique_together = ("driver", "day_of_week")
        ordering = ["driver", "day_of_week"]

    def __str__(self):
        day_name = dict(self.DAY_CHOICES).get(self.day_of_week, "?")
        if not self.is_available:
            return f"{self.driver} — {day_name}: OFF"
        return f"{self.driver} — {day_name}: {self.start_hour}:00–{self.end_hour}:00"


class DriverDateOverride(models.Model):
    """One-time availability exception. Takes priority over weekly schedule.

    Supports:
      - Single-day OFF (legacy default).
      - Multi-day off requests (date + end_date).
      - Partial-day windows (available_until / available_after / available_window /
        unavailable_window) using start_time / end_time.
      - Flexible override (driver works even though normally off).
      - Note-only entries (no schedule change, just dispatcher info).
    """
    REASON_CHOICES = [
        ("day_off", "Day Off"),
        ("vacation", "Vacation"),
        ("sick", "Sick"),
        ("appointment", "Appointment"),
        ("other", "Other"),
    ]
    EXCEPTION_TYPE_CHOICES = [
        ("off",                 "Off (full day)"),
        ("available_until",     "Available until time"),
        ("available_after",     "Available after time"),
        ("available_window",    "Available only during window"),
        ("unavailable_window",  "Unavailable during window"),
        ("flexible",            "Flexible (override off-day)"),
        ("note_only",           "Note only"),
    ]
    STATUS_CHOICES = [
        ("approved", "Approved"),
        ("pending",  "Pending review"),
        ("denied",   "Denied"),
        ("cancelled","Cancelled by driver"),
    ]

    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="date_overrides")
    date = models.DateField(help_text="First day this exception applies.")
    end_date = models.DateField(
        null=True, blank=True,
        help_text="Last day this exception applies. Leave blank for a single-day exception."
    )
    exception_type = models.CharField(
        max_length=24, choices=EXCEPTION_TYPE_CHOICES, default="off",
        help_text="What kind of exception this is."
    )
    start_time = models.TimeField(
        null=True, blank=True,
        help_text="For partial-day exceptions: the start of the affected window."
    )
    end_time = models.TimeField(
        null=True, blank=True,
        help_text="For partial-day exceptions: the end of the affected window."
    )
    is_available = models.BooleanField(
        default=False,
        help_text="Derived: False for full-day Off; True for partial-day or flexible exceptions."
    )
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default="day_off")
    notes = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="created_driver_overrides",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Approval workflow. Dispatcher/founder-created rows default to "approved"
    # so existing data and admin behavior are unchanged. Driver self-service
    # submissions land as "pending" and only affect availability once approved.
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default="approved",
        help_text="Approval state. Only 'approved' rows affect schedule availability."
    )
    submitted_by_driver = models.BooleanField(
        default=False,
        help_text="True when the driver submitted this themselves via the driver portal."
    )
    decided_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="decided_driver_overrides",
        help_text="Founder/dispatcher who approved or denied this request."
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    denial_reason = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Shown to the driver when their request is denied."
    )

    class Meta:
        ordering = ["date", "driver"]

    def save(self, *args, **kwargs):
        # Keep is_available in sync with exception_type so older code paths
        # (admin filters, auto-assigner cascade) keep working.
        self.is_available = self.exception_type != "off"
        super().save(*args, **kwargs)

    @property
    def is_pending(self):
        return self.status == "pending"

    def applies_on(self, target_date):
        if self.end_date is None:
            return self.date == target_date
        return self.date <= target_date <= self.end_date

    @property
    def date_range_display(self):
        if self.end_date is None or self.end_date == self.date:
            return self.date.strftime("%b %d, %Y")
        if self.date.year == self.end_date.year:
            return f"{self.date.strftime('%b %d')} – {self.end_date.strftime('%b %d, %Y')}"
        return f"{self.date.strftime('%b %d, %Y')} – {self.end_date.strftime('%b %d, %Y')}"

    def __str__(self):
        type_label = dict(self.EXCEPTION_TYPE_CHOICES).get(self.exception_type, self.exception_type)
        return f"{self.driver} — {self.date_range_display}: {type_label}"

    @classmethod
    def find_duplicate(
        cls, driver, date, end_date, exception_type,
        start_time=None, end_time=None, exclude_id=None,
    ):
        """Return an existing pending/approved override matching the same
        driver + date window + exception type, or None.

        Used by both the dispatcher and driver-self-serve creation paths to
        squash accidental double-submissions (the kind that happen when a
        save button is double-clicked or a form re-POSTs on back/forward).
        Denied/cancelled rows are ignored so a redo after a denial still
        works.
        """
        qs = cls.objects.filter(
            driver=driver,
            date=date,
            exception_type=exception_type,
            status__in=("pending", "approved"),
        )
        if end_date is None:
            qs = qs.filter(end_date__isnull=True)
        else:
            qs = qs.filter(end_date=end_date)
        if start_time is not None:
            qs = qs.filter(start_time=start_time)
        if end_time is not None:
            qs = qs.filter(end_time=end_time)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        return qs.order_by("id").first()

    def duplicate_group(self):
        """All non-cancelled-non-denied overrides this row is a duplicate of
        (including self). Used by the dispatcher queue to display "3 dup
        submissions" and to cascade approve/deny across the whole batch.
        """
        return type(self).objects.filter(
            driver=self.driver,
            date=self.date,
            end_date=self.end_date,
            exception_type=self.exception_type,
            start_time=self.start_time,
            end_time=self.end_time,
            status__in=("pending", "approved"),
        ).order_by("id")


class FleetVehicle(models.Model):
    # A live GPS sample older than this is considered stale (the vehicle is
    # mapped to Samsara but we're not getting fresh telemetry).
    SAMSARA_FRESH_MINUTES = 15

    vehicle_number = models.CharField(max_length=50, unique=True)
    vehicle_type = models.ForeignKey(
        "rates.Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Vehicle type/tier for scheduling (e.g. SUV, Van, MiniVan)"
    )
    year = models.PositiveIntegerField()
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(
        default=True, db_index=True,
        help_text="Inactive vehicles can't be assigned and are hidden from "
                  "selection/availability surfaces. Legs already on the vehicle "
                  "still render so history is preserved."
    )

    # --- Per-unit scheduling capacity cap (opt-in) ---
    # SCHEDULER-ONLY: caps what the auto-assign / schedule builder will put on THIS
    # physical car, even when the booked vehicle TYPE (rates.Vehicle) allows more.
    # Used for an odd unit out — e.g. an SUV with less seat/cargo room than the rest
    # of the SUV tier. NULL on either field = inherit the type default; no cap applied.
    # Does NOT touch booking validation, pricing, or the customer-facing type capacity.
    max_passenger_capacity = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Scheduler-only passenger cap for this specific unit. "
                  "NULL = use the vehicle type's capacity (no per-unit cap)."
    )
    max_luggage_capacity = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Scheduler-only suitcase cap for this specific unit. "
                  "NULL = use the vehicle type's luggage capacity (no per-unit cap)."
    )

    # --- Samsara telematics (Phase 1: read-only live vehicle visibility) ---
    # All nullable/blank so un-onboarded in-house cars and affiliate vehicles
    # are unaffected. Written ONLY by the background poller (samsara_scheduler);
    # never read synchronously from the API in a request path.
    samsara_vehicle_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text="Samsara vehicle id. Blank = not onboarded; renders no live position."
    )
    samsara_last_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    samsara_last_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    samsara_last_location_label = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Reverse-geocoded label from Samsara (e.g. 'near MCO Terminal A')."
    )
    samsara_movement_status = models.CharField(
        max_length=32, blank=True, default="",
        help_text="driving / idle (derived from Samsara speed)."
    )
    samsara_last_seen_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Timestamp of the Samsara GPS sample itself."
    )
    samsara_last_synced_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When we last successfully polled Samsara for this vehicle (diagnostic)."
    )
    samsara_stationary_since = models.DateTimeField(
        null=True, blank=True,
        help_text="When the vehicle last stopped moving (cleared when it drives). "
                  "Drives the 'vehicle not moving' dwell detection.",
    )

    # --- Vehicle master identity (Fleet Management) ----------------------
    # VIN and plate were already being fetched from Samsara's /fleet/vehicles and
    # printed to stdout by samsara_sync_vehicles --list-mappings, then discarded.
    # Storing them buys two things: auto-mapping on onboarding, and a SECOND
    # identity to check the samsara_vehicle_id against — a gateway moved between
    # cars is otherwise silent and re-attributes all history.
    vin = models.CharField(
        max_length=17, blank=True, default="", db_index=True,
        help_text="17-char VIN. Synced from Samsara where available; blank = unknown.",
    )
    license_plate = models.CharField(
        max_length=16, blank=True, default="",
        help_text="Plate as Samsara reports it. Identification/display only.",
    )
    samsara_name = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Samsara's own label for this vehicle. Shown beside vehicle_number "
                  "so a mis-typed samsara_vehicle_id is obvious at a glance.",
    )

    # --- Toll transponder -------------------------------------------------
    # Central Florida runs on tolls (417/408/528 to MCO, the Beachline to Port
    # Canaveral), so "which transponder is in which car" is a question that gets
    # asked when a toll bill needs reconciling or a unit swaps drivers.
    # Type is stored because both SunPass and E-PASS are common here and they
    # bill through different accounts.
    TRANSPONDER_TYPE_CHOICES = [
        ("sunpass", "SunPass"),
        ("epass", "E-PASS"),
        ("other", "Other"),
    ]
    transponder_number = models.CharField(
        max_length=32, blank=True, default="", db_index=True,
        help_text="Transponder / account number mounted in this vehicle. "
                  "Blank = none assigned.",
    )
    transponder_type = models.CharField(
        max_length=16, choices=TRANSPONDER_TYPE_CHOICES, blank=True, default="",
        help_text="Which toll network this transponder bills through.",
    )

    # --- Compliance dates (manual; no Samsara involvement) ---------------
    # Deliberately NOT a status enum. There is no such thing as a car that
    # 'can't work today' in this operation (dispatching/day_setup.py:33-36) —
    # these are dates that drive a warning label, never a capacity subtraction.
    in_service_since = models.DateField(null=True, blank=True)
    registration_expires_on = models.DateField(null=True, blank=True)
    insurance_expires_on = models.DateField(null=True, blank=True)
    next_inspection_on = models.DateField(null=True, blank=True)

    # --- Out of service (manual, human-set, DOES gate scheduling) ---------
    # Read the rule above before touching this. "Readiness is advisory, always"
    # (docs/fleet-management.md) is about MACHINE inference — a readiness chip or
    # fault code deciding a car can't work. Guard A died because stale per-vehicle
    # telemetry produced false positives, and that reasoning still stands.
    #
    # This is a different class of fact: a human who knows the car is on a lift
    # says so, by hand, with a reason. It cannot be stale in the way a sensor is,
    # and the person setting it is the person who knows. So this one — and ONLY
    # this one — is allowed to remove a unit from the pool. The block is
    # overridable at assignment time precisely so a wrong flag can never strand a
    # car that came back early.
    #
    # Date-windowed rather than a boolean, because the planner schedules FUTURE
    # dates: a car in the shop this week must still be assignable next week.
    # NULL `from`      = in service.
    # `from`, no `until` = down indefinitely (no ETA).
    # `from` + `until`   = a closed window; the car is back on `until` + 1 day.
    # (VehicleServiceRecord.out_of_service_from/to is the HISTORICAL log of a
    # service that happened. This is the live scheduling state. Different jobs.)
    out_of_service_from = models.DateField(
        null=True, blank=True, db_index=True,
        help_text="First date the unit is unavailable. Blank = in service.",
    )
    out_of_service_until = models.DateField(
        null=True, blank=True,
        help_text="Last date unavailable (inclusive). Blank with a start date "
                  "means down indefinitely — no return date known.",
    )
    out_of_service_reason = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Why it's down, in the dispatcher's words — 'transmission, at "
                  "Bob's', 'rear-ended 8/3'. Shown wherever the unit is blocked.",
    )

    # --- Operating permits ------------------------------------------------
    # Central Florida ground transport is permitted per VEHICLE, not per company:
    # GOAA decals for MCO, Sanford's own permit for SFB, and Port Canaveral's for
    # cruise pickups. A unit without the decal can be sent to DROP there, but
    # picking up is what needs the permit.
    #
    # ADVISORY by explicit decision: a missing permit warns, it never blocks.
    # Pickup locations are free text matched by categorize_location(), and MCO is
    # most of the business — a hard block would misfire on the busiest lane. The
    # dispatcher gets a named warning and makes the call.
    #
    # Flat fields rather than a related model: three fixed permits, matching the
    # compliance-date style directly above, and no prefetch on any pool render.
    # A fourth permit is a migration — that's the accepted trade.
    permit_mco = models.BooleanField(
        default=False, help_text="Holds a GOAA / MCO pickup permit.")
    permit_mco_expires_on = models.DateField(null=True, blank=True)
    permit_sanford = models.BooleanField(
        default=False, help_text="Holds a Sanford (SFB) pickup permit.")
    permit_sanford_expires_on = models.DateField(null=True, blank=True)
    permit_port_canaveral = models.BooleanField(
        default=False, help_text="Holds a Port Canaveral pickup permit.")
    permit_port_canaveral_expires_on = models.DateField(null=True, blank=True)

    # --- Latest telematics (Fleet Management) ----------------------------
    # Same contract as the samsara_* block above: written ONLY by the background
    # poller, never from a request path, all nullable so an un-onboarded car or a
    # GPS-only asset gateway simply leaves them empty. An absent reading must
    # NEVER overwrite a good stored value with NULL.
    samsara_odometer_meters = models.DecimalField(
        max_digits=14, decimal_places=1, null=True, blank=True,
        help_text="Latest odometer in METERS (canonical unit). NULL = never reported.",
    )
    samsara_odometer_source = models.CharField(
        max_length=8, blank=True, default="",
        help_text="Which counter produced it: obd (exact) / gps (estimate). "
                  "Stored, never inferred at render — see dispatching/mileage.py.",
    )
    samsara_odometer_at = models.DateTimeField(
        null=True, blank=True, help_text="Samsara's timestamp for the odometer sample."
    )
    samsara_gps_distance_meters = models.DecimalField(
        max_digits=14, decimal_places=1, null=True, blank=True,
        help_text="Cumulative gpsDistanceMeters. The mileage FALLBACK baseline.",
    )
    samsara_fuel_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    samsara_battery_millivolts = models.PositiveIntegerField(null=True, blank=True)
    samsara_engine_state = models.CharField(
        max_length=16, blank=True, default="",
        help_text="Running / Idle / Off, as Samsara reports it.",
    )
    samsara_engine_seconds = models.BigIntegerField(
        null=True, blank=True,
        help_text="Engine hours in seconds (obdEngineSeconds). Frequently absent on "
                  "light-duty OBD-II — populated opportunistically, nothing is built "
                  "on it. Do not add a feature that requires this without checking "
                  "coverage first (manage.py fleet_probe).",
    )
    samsara_open_fault_count = models.PositiveSmallIntegerField(null=True, blank=True)
    samsara_faults_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["vehicle_number"]
        constraints = [
            # A duplicate id silently maps two cars to one GPS feed: the poller
            # builds {samsara_vehicle_id: vehicle} and the last row wins, so the
            # other car goes dark with no error. Partial so the 3 un-onboarded
            # units (and every future one) can keep sharing the "" default —
            # partial coverage is the designed steady state, not a backlog.
            models.UniqueConstraint(
                fields=["samsara_vehicle_id"],
                condition=~Q(samsara_vehicle_id=""),
                name="uniq_fleetvehicle_samsara_vehicle_id",
            ),
        ]

    def __str__(self):
        return f"{self.vehicle_number} - {self.year} {self.make} {self.model}"

    @property
    def samsara_enabled(self) -> bool:
        """True only when this car is mapped to a Samsara vehicle."""
        return bool(self.samsara_vehicle_id)

    @property
    def samsara_is_fresh(self) -> bool:
        """True when we have a GPS sample within the freshness window."""
        if not self.samsara_last_seen_at:
            return False
        return (timezone.now() - self.samsara_last_seen_at) <= timedelta(
            minutes=self.SAMSARA_FRESH_MINUTES
        )

    def samsara_age_display(self) -> str:
        """Compact age of the last GPS sample, e.g. '2m ago' / '1h 3m ago'."""
        if not self.samsara_last_seen_at:
            return ""
        total_min = int((timezone.now() - self.samsara_last_seen_at).total_seconds() // 60)
        if total_min <= 0:
            return "just now"
        if total_min < 60:
            return f"{total_min}m ago"
        return f"{total_min // 60}h {total_min % 60}m ago"

    @property
    def odometer_miles(self):
        """
        Latest odometer in miles, or None when we have never had a reading.

        None is NOT zero — templates must render it as an em-dash. A car with a
        GPS-only gateway legitimately never gets one, and showing 0 would make it
        look brand new.
        """
        from dispatching.mileage import meters_to_miles

        return meters_to_miles(self.samsara_odometer_meters, places=0)

    @property
    def odometer_is_estimate(self) -> bool:
        """True when the stored odometer came from GPS distance, not the OBD bus."""
        return self.samsara_odometer_source == "gps"

    # ── Out of service ───────────────────────────────────────────────────
    def is_out_of_service_on(self, day) -> bool:
        """Is this unit unavailable on ``day``?

        Asked per-date, never "right now", because every scheduling surface works
        on a chosen date — a unit in the shop this week is fine next week.
        """
        if not self.out_of_service_from or day is None:
            return False
        if day < self.out_of_service_from:
            return False
        if self.out_of_service_until and day > self.out_of_service_until:
            return False
        return True

    @property
    def is_out_of_service_now(self) -> bool:
        return self.is_out_of_service_on(timezone.localdate())

    def out_of_service_label(self, day=None) -> str:
        """One line naming the reason and the return date, for the pool and the
        blocked-assignment message. Empty when the unit is available."""
        day = timezone.localdate() if day is None else day
        if not self.is_out_of_service_on(day):
            return ""
        reason = self.out_of_service_reason.strip() or "Out of service"
        if self.out_of_service_until:
            back = self.out_of_service_until + timedelta(days=1)
            return f"{reason} — back {back.strftime('%a %b %-d')}"
        return f"{reason} — no return date"

    # ── Permits ──────────────────────────────────────────────────────────
    # categorize_location() values -> the permit that covers picking up there.
    PERMITS = (
        ("mco", "MCO", "MCO Terminal"),
        ("sanford", "Sanford", "SFB Terminal"),
        ("port_canaveral", "Port Canaveral", "Port Canaveral Area"),
    )

    def permits(self, day=None):
        """Every permit as a row for display: held, expiry, and whether it lapsed.

        An EXPIRED permit is reported as not held — a lapsed decal is worth
        exactly as much as no decal, and showing it as a tick with a red date
        invites someone to skim past it.
        """
        day = timezone.localdate() if day is None else day
        rows = []
        for key, label, location_category in self.PERMITS:
            expires_on = getattr(self, f"permit_{key}_expires_on")
            expired = bool(expires_on and expires_on < day)
            on_file = getattr(self, f"permit_{key}")
            rows.append({
                "key": key,
                "label": label,
                "location_category": location_category,
                "on_file": on_file,
                "expires_on": expires_on,
                "expired": expired,
                "valid": bool(on_file and not expired),
            })
        return rows

    def permit_for_location(self, location_category, day=None):
        """The permit row covering pickups at ``location_category``, or None when
        that location needs no permit (a resort, a residence)."""
        for row in self.permits(day=day):
            if row["location_category"] == location_category:
                return row
        return None

    def missing_permit_for_pickup(self, pickup_location, day=None):
        """The permit row this unit LACKS for picking up at ``pickup_location``,
        or None when it's covered (or the location needs no permit).

        Pickup only — a car with no decal may legally drop at any of these.
        """
        from dispatching.analytics import categorize_location

        row = self.permit_for_location(categorize_location(pickup_location), day=day)
        return None if row is None or row["valid"] else row


class DriverVehicleAssignment(models.Model):
    driver = models.ForeignKey(
        "Driver", on_delete=models.CASCADE, related_name="vehicle_assignments"
    )
    date = models.DateField()
    vehicle = models.ForeignKey(
        "FleetVehicle", on_delete=models.PROTECT, null=True, blank=True
    )
    # Planned working window for THIS day's assignment (Day Setup shared cars: two drivers
    # on one unit get partitioned windows, e.g. AM 4-15 / PM 15-23, so the auto-assign
    # modal prefills the split and the engine can never double-book the vehicle).
    # NULL = no planned window (normal single-driver assignment; saved availability rules).
    planned_start_hour = models.PositiveSmallIntegerField(null=True, blank=True)
    planned_end_hour = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("driver", "date")
        ordering = ["date", "driver"]

    def __str__(self):
        return f"{self.driver} - {self.vehicle} ({self.date})"


class DriverPushSubscription(models.Model):
    """A browser/device Web Push subscription for a driver's portal session.
    One driver can hold several (phone + tablet). Dead subscriptions (the push
    service answers 404/410) are pruned automatically by the send helper."""

    driver = models.ForeignKey(
        "Driver", on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.driver} push ({self.endpoint[:40]}…)"


def _wakeup_token():
    import secrets
    return secrets.token_urlsafe(24)


class DriverWakeupCheck(models.Model):
    """One row per (driver, service date) tracking the early-morning wake-up
    escalation ladder: confirm-link SMS → wake-up phone call → owner alert.

    Created by the background sweeper (drivers/wakeup.py) only for in-house
    drivers whose FIRST pickup of the day is before
    settings.WAKEUP_EARLY_CUTOFF_HOUR. Step timestamps mean "attempted at" —
    the ladder is time-driven, so a failed Twilio send never blocks the next
    rung (failures land in `log`).
    """

    STATUS_PENDING = "pending"
    STATUS_ACKED = "acked"
    STATUS_ESCALATED = "escalated"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACKED, "Confirmed awake"),
        (STATUS_ESCALATED, "Escalated to owners"),
        (STATUS_CANCELLED, "Cancelled (no longer an early first pickup)"),
    ]

    driver = models.ForeignKey(
        "Driver", on_delete=models.CASCADE, related_name="wakeup_checks"
    )
    date = models.DateField(db_index=True, help_text="Service date of the early first pickup.")
    leg = models.ForeignKey(
        Leg, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="wakeup_checks",
        help_text="The first leg the check was built around (informational).",
    )
    first_pickup_at = models.DateTimeField(
        help_text="The driver's first pickup — every deadline is relative to this. "
                  "Re-synced each sweep while the check is unacknowledged."
    )
    token = models.CharField(
        max_length=64, unique=True, default=_wakeup_token,
        help_text="Capability token for the no-login tap-to-confirm link.",
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    sms_sent_at = models.DateTimeField(null=True, blank=True)
    push_sent_at = models.DateTimeField(null=True, blank=True)
    call_started_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    acked_at = models.DateTimeField(null=True, blank=True)
    ack_source = models.CharField(
        max_length=12, blank=True, default="",
        help_text="link (tapped the SMS link), call (pressed a key), admin (manual).",
    )
    log = models.TextField(blank=True, default="", help_text="Timestamped breadcrumbs per step.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("driver", "date")
        ordering = ["-date", "first_pickup_at"]

    def __str__(self):
        return f"Wake-up {self.driver} {self.date} ({self.status})"


class DriverPayRate(models.Model):
    """Per-driver pay rate. Handles inhouse overrides and affiliate rates.

    For INHOUSE: vehicle is NULL (pay is route-based, same for all vehicles).
    For AFFILIATE: vehicle can be set for vehicle-specific rates, or NULL for all vehicles.
    direction handles directional rates without duplicating Route records.
    """
    DIRECTION_CHOICES = [
        ("both", "Both directions"),
        ("forward", "Forward (origin → dest)"),
        ("reverse", "Reverse (dest → origin)"),
    ]

    driver = models.ForeignKey(
        "Driver", on_delete=models.CASCADE, related_name="pay_rates"
    )
    route = models.ForeignKey(
        "rates.Route", on_delete=models.CASCADE, related_name="driver_pay_rates"
    )
    vehicle = models.ForeignKey(
        "rates.Vehicle", on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="driver_pay_rates",
        help_text="NULL = same rate for all vehicles on this route."
    )
    direction = models.CharField(
        max_length=10, choices=DIRECTION_CHOICES, default="both",
        help_text="'both' = same rate both ways. 'forward' = origin→dest. 'reverse' = dest→origin."
    )
    base_pay = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Base pay for this driver/route/vehicle/direction combo"
    )

    class Meta:
        unique_together = ("driver", "route", "vehicle", "direction")
        ordering = ["driver", "route"]
        indexes = [
            models.Index(fields=["driver", "route"]),
        ]

    def __str__(self):
        arrows = {"both": "↔", "forward": "→", "reverse": "←"}
        veh = f" / {self.vehicle}" if self.vehicle else ""
        return f"{self.driver} - {self.route} {arrows.get(self.direction, '↔')}{veh}: ${self.base_pay}"


class AffiliateProfile(models.Model):
    """Architecture B: per-affiliate CAPABILITY / CAPACITY / route-permit config for the Farm-Out
    Opportunity-Cost Optimizer (``dispatching/farmout_optimizer.py``). The optimizer is read-only;
    this model is the ONLY persisted config it consults — rates already live in ``DriverPayRate``.

    Turns into DATA the facts that were HARDCODED during the Waleed-only validation pass:
      * Waleed's "SUV-or-lower" capability    -> ``max_vehicle_tier``
      * Anthony's 12-legs/day cap             -> ``capacity_mode='count_cap'`` + ``daily_cap``
      * Oualid's single-vehicle chain         -> ``capacity_mode='single_chain'``
      * Waleed's "Port/Sanford drop-off only" -> ``no_pickup_at_port_sanford``

    Only AFFILIATE drivers get a profile. A carded affiliate with NO profile is still priced by their
    ``DriverPayRate`` card but with NO capability cap — safe for PER-VEHICLE cards (which gate
    themselves by which vehicle rows exist), RISKY for FLAT all-vehicle cards (one NULL-vehicle row
    matches every class), so set ``max_vehicle_tier`` for those. The optimizer surfaces profile-less
    carded affiliates for review rather than guessing.
    """
    CAP_SINGLE_CHAIN = "single_chain"
    CAP_COUNT = "count_cap"
    CAP_FLEET = "fleet"
    CAPACITY_MODE_CHOICES = [
        (CAP_SINGLE_CHAIN, "Single vehicle (one feasibility chain, no count cap)"),
        (CAP_COUNT, "Daily leg-count cap (finite seats/day)"),
        (CAP_FLEET, "Fleet (treated as a higher count cap; true parallel chains deferred)"),
    ]
    # Values MUST equal scheduler.VEHICLE_TIER_ORDER entries exactly — the engine resolves the
    # cap with a case-sensitive index into that list (a free-typed 'SUV' used to silently
    # exclude the affiliate from everything). Choices make the admin a dropdown.
    VEHICLE_TIER_CHOICES = [
        ("", "— no cap (rate card alone gates; vans still need an explicit van rate row)"),
        ("towncar", "Towncar"),
        ("mini_van", "Mini Van"),
        ("suv", "SUV"),
        ("van", "Van"),
        ("Van(14 Pax)", "Van (14 Pax)"),
    ]

    driver = models.OneToOneField(
        "Driver", on_delete=models.CASCADE, related_name="affiliate_profile",
        help_text="The affiliate this config describes (driver_type='affiliate').",
    )
    max_vehicle_tier = models.CharField(
        max_length=20, blank=True, default="", choices=VEHICLE_TIER_CHOICES,
        help_text="Highest vehicle class this affiliate can serve. Blank = no capability cap "
                  "(the rate card alone gates eligibility — but van/14-pax jobs then require an "
                  "explicit van rate row; a flat all-vehicle card never auto-claims them).",
    )
    capacity_mode = models.CharField(
        max_length=20, choices=CAPACITY_MODE_CHOICES, default=CAP_SINGLE_CHAIN,
        help_text="How the optimizer rations this affiliate's daily capacity.",
    )
    daily_cap = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Legs/day for count_cap (e.g. Anthony 12) or fleet capacity for fleet mode. "
                  "Ignored for single_chain (capacity = the feasibility chain). NULL = unlimited count.",
    )
    no_pickup_at_port_sanford = models.BooleanField(
        default=False,
        help_text="Affiliate may DROP at Port Canaveral / Sanford but never PICK UP there (no permit). "
                  "Excludes any leg ORIGINATING at Port/Sanford from this affiliate (Waleed's rule).",
    )
    notes = models.TextField(
        blank=True, default="",
        help_text="Internal notes about this affiliate's capability / capacity / permits.",
    )

    class Meta:
        ordering = ["driver"]

    def __str__(self):
        cap = self.max_vehicle_tier or "any-class"
        return f"AffiliateProfile({self.driver} · {cap} · {self.capacity_mode})"


class DriverPayment(models.Model):
    """
    Tracks batches of payments made to drivers
    """

    driver = models.ForeignKey(
        "Driver", on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, help_text="Total payment (base_pay + gratuity + additional)")
    base_pay = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total base pay amount (excluding gratuity)",
    )
    gratuity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total gratuity amount",
    )
    additional = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total additional pay amount (e.g., wait time, early morning bonus, etc.)",
    )
    payment_date = models.DateTimeField(auto_now_add=True)
    payment_method = models.CharField(max_length=50, default="direct deposit")
    reference_number = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    # Track who created this payment
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_driver_payments",
    )

    class Meta:
        ordering = ["-payment_date"]

    def __str__(self):
        return f"Payment {self.id} to {self.driver}: ${self.amount}"

    @classmethod
    def create_payment(
        cls,
        driver,
        legs,
        payment_method="direct deposit",
        reference_number="",
        notes="",
        created_by=None,
    ):
        """
        Create a payment for multiple legs at once
        """
        from django.db import transaction
        import logging

        logger = logging.getLogger(__name__)

        with transaction.atomic():
            # Calculate totals - prefer new fields, fallback to legacy field
            total_base_pay = Decimal("0.00")
            total_gratuity = Decimal("0.00")
            total_additional = Decimal("0.00")
            total_amount = Decimal("0.00")
            
            for leg in legs:
                # Use new fields if available, otherwise fallback to driver_pay_amount
                if leg.driver_base_pay is not None or leg.driver_gratuity is not None or leg.driver_additional is not None:
                    leg_base = leg.driver_base_pay or Decimal("0.00")
                    leg_gratuity = leg.driver_gratuity or Decimal("0.00")
                    leg_additional = leg.driver_additional or Decimal("0.00")
                    total_base_pay += leg_base
                    total_gratuity += leg_gratuity
                    total_additional += leg_additional
                    total_amount += leg_base + leg_gratuity + leg_additional
                else:
                    # Fallback to legacy field
                    leg_amount = leg.driver_pay_amount or Decimal("0.00")
                    total_amount += leg_amount
                    # If we only have the total, we can't split it, so leave base_pay/gratuity/additional as None
                    # They will be calculated from leg_payments later if needed

            # Log payment creation
            logger.info(
                f"Creating payment for driver {driver} with {len(legs)} legs. "
                f"Total: ${total_amount}, Base: ${total_base_pay}, Gratuity: ${total_gratuity}, Additional: ${total_additional}"
            )

            # Create the payment
            payment = cls.objects.create(
                driver=driver,
                amount=total_amount,
                base_pay=total_base_pay if total_base_pay > 0 else None,
                gratuity=total_gratuity if total_gratuity > 0 else None,
                additional=total_additional if total_additional > 0 else None,
                payment_method=payment_method,
                reference_number=reference_number,
                notes=notes,
                created_by=created_by,
            )

            logger.info(f"Created payment ID: {payment.id}")

            # Build leg payment records in bulk
            leg_payment_objects = []
            leg_ids = []
            for leg in legs:
                if leg.driver_base_pay is not None or leg.driver_gratuity is not None or leg.driver_additional is not None:
                    leg_base = leg.driver_base_pay or Decimal("0.00")
                    leg_gratuity = leg.driver_gratuity or Decimal("0.00")
                    leg_additional = leg.driver_additional or Decimal("0.00")
                    leg_amount = leg_base + leg_gratuity + leg_additional
                else:
                    leg_amount = leg.driver_pay_amount or Decimal("0.00")
                    leg_base = None
                    leg_gratuity = None
                    leg_additional = None

                logger.info(
                    f"Processing leg ID: {leg.id}, Amount: ${leg_amount}, "
                    f"Base: ${leg_base}, Gratuity: ${leg_gratuity}, Additional: ${leg_additional}"
                )

                leg_payment_objects.append(LegPayment(
                    payment=payment,
                    leg=leg,
                    amount=leg_amount,
                    base_pay=leg_base,
                    gratuity=leg_gratuity,
                    additional=leg_additional,
                ))
                leg_ids.append(leg.id)

            # Bulk insert all LegPayment records (1 INSERT instead of N)
            LegPayment.objects.bulk_create(leg_payment_objects)
            logger.info(f"Bulk-created {len(leg_payment_objects)} LegPayment records")

            # Bulk update all legs to paid status (1 UPDATE instead of N)
            Leg.objects.filter(id__in=leg_ids).update(payment_status="paid")
            logger.info(f"Updated {len(leg_ids)} legs to paid status")

            # Verify all LegPayment records were created
            payment_refresh = cls.objects.get(id=payment.id)
            leg_payment_count = payment_refresh.leg_payments.count()

            if leg_payment_count != len(legs):
                logger.error(
                    f"MISMATCH: Expected {len(legs)} leg payments but found {leg_payment_count}"
                )
                # This doesn't need to be raised as an exception - just log it
            else:
                logger.info(
                    f"Successfully created {leg_payment_count} leg payment records"
                )

            return payment


class LegPayment(models.Model):
    """
    Links a payment to the specific legs that were paid
    """

    STATUS_ACTIVE = "active"
    STATUS_VOIDED = "voided"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_VOIDED, "Voided"),
    ]

    payment = models.ForeignKey(
        DriverPayment, on_delete=models.CASCADE, related_name="leg_payments"
    )
    leg = models.ForeignKey(
        Leg, on_delete=models.PROTECT, related_name="payment_records"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, help_text="Total payment (base_pay + gratuity + additional)")
    base_pay = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Base pay amount for this leg (excluding gratuity)",
    )
    gratuity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Gratuity amount for this leg",
    )
    additional = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Additional pay amount for this leg (e.g., wait time, early morning bonus, etc.)",
    )

    # ── Adjustment tracking ──
    # `status` is the source of truth for "is this line counted as paid?"
    # Voided lines stay in the table for history but are excluded from
    # statement totals, the customer-facing email, and the Gusto export.
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True,
    )
    original_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="The line's amount at the moment of processing. Captured on first edit so subsequent edits don't lose the initial value.",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="voided_leg_payments",
    )
    void_reason = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="updated_leg_payments",
    )

    class Meta:
        unique_together = ("payment", "leg")

    def __str__(self):
        return f"Payment {self.payment.id} - Leg {self.leg.id}"


class DriverPayoutAdjustment(models.Model):
    """
    Append-only audit row for every line-level correction made to a
    processed DriverPayment statement.

    One row is written each time staff voids a line, edits its amount,
    or adds a missing leg. The row records who/when/why plus enough
    state (old amount, new amount, signed delta) to reconstruct the
    correction even if the linked LegPayment is later edited again.

    These rows are NOT a separate "billing line" rolled into the next
    statement. Voided legs return to the unpaid queue naturally and
    flow through the existing payroll flow next period.
    """

    TYPE_VOID = "void_line"
    TYPE_EDIT = "edit_amount"
    TYPE_ADD = "add_missing_leg"
    TYPE_CHOICES = [
        (TYPE_VOID, "Void line"),
        (TYPE_EDIT, "Edit amount"),
        (TYPE_ADD, "Add missing leg"),
    ]

    payment = models.ForeignKey(
        DriverPayment, on_delete=models.CASCADE, related_name="adjustments",
    )
    leg_payment = models.ForeignKey(
        LegPayment, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="adjustments",
    )
    leg = models.ForeignKey(
        Leg, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="payout_adjustments",
        help_text="Denormalized — same as leg_payment.leg when present; "
                  "set directly for add_missing_leg.",
    )
    adjustment_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    old_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Line amount before this adjustment. Null for add_missing_leg.",
    )
    new_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Line amount after this adjustment. Null for void_line.",
    )
    delta = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Signed change applied to DriverPayment.amount.",
    )
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="created_payout_adjustments",
    )
    # Snapshot whether the statement had already been emailed / exported
    # at the time of the correction — useful for "did the driver see the
    # old total?" questions when reading the audit log later.
    statement_was_emailed = models.BooleanField(default=False)
    statement_was_exported = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["payment", "-created_at"]),
        ]

    def __str__(self):
        return (
            f"{self.get_adjustment_type_display()} on payment #{self.payment_id} "
            f"({self.delta:+.2f}) by {self.created_by_id}"
        )


class DriverPaymentExport(models.Model):
    """
    Audit record for each Gusto Smart Import CSV download.

    This does NOT mark anything as "paid in Gusto" — staff still upload the CSV
    manually. The record exists so the team can answer "did we already export
    this payroll period, who did it, and which DriverPayment IDs were in it?"
    Re-exports are intentionally allowed.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="gusto_csv_exports",
    )
    from_date = models.DateField(help_text="Payroll period start (Monday typically).")
    to_date = models.DateField(help_text="Payroll period end (Sunday typically).")
    csv_file_name = models.CharField(max_length=255)
    selected_driver_count = models.PositiveIntegerField(default=0)
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
    )
    exported_payment_ids = models.JSONField(
        default=list, blank=True,
        help_text="List of DriverPayment IDs included in this CSV."
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Driver payment Gusto export"
        verbose_name_plural = "Driver payment Gusto exports"

    def __str__(self):
        return (
            f"Gusto export {self.from_date}→{self.to_date} "
            f"({self.selected_driver_count} drivers, ${self.total_amount})"
        )


# ════════════════════════════════════════════════════════════════════════════
# FLEET MANAGEMENT
#
# All five models live here in drivers/models.py rather than in a new app. Two
# reasons: they all hang off FleetVehicle, and one app keeps the migration
# collision surface to a single file — Samsara migrations have already collided
# across branches once (the abandoned `samsara` branch carries a
# samsara_integration/0001 depending on a drivers/0025 that never existed on
# main). Do not resurrect that app.
# ════════════════════════════════════════════════════════════════════════════


class VehicleDayReading(models.Model):
    """
    One row per vehicle per LOCAL day: the odometer at both ends and the miles
    between them.

    Derived and re-runnable, never accumulated. The nightly recomputes
    `miles_driven` from the stored start/end every time it runs, so running it
    twice produces the same row — there is no `miles += delta` anywhere, because
    an accumulator can never be repaired once it drifts.

    `miles_driven = NULL` means UNKNOWN and must render as an em-dash. Zero means
    the car provably did not move. Conflating them makes a dead gateway look like
    a parked car and poisons every total above it, so any aggregate must state
    its coverage ("1,842 mi across 26 of 31 days").

    Backfill: nothing in OUR database can reconstruct a past day — Samsara GPS is
    overwritten every 3 minutes and was never historized here
    (docs/operational-data-audit.md). But Samsara's own /fleet/vehicles/stats/history
    endpoint IS entitled on this account (confirmed by manage.py fleet_probe,
    2026-08-05), so a date-window backfill is possible and is the right way to
    repair a gap — re-pull from the vendor rather than carry our own sample
    archive. Until that command exists, this table accrues forward only.
    """

    vehicle = models.ForeignKey(
        FleetVehicle, on_delete=models.PROTECT, related_name="day_readings"
    )
    date = models.DateField(
        db_index=True,
        help_text="LOCAL service date (America/New_York). Always derive with "
                  "timezone.localdate() — USE_TZ is on, so a naive UTC date would "
                  "put the 8pm-to-midnight window on the wrong day.",
    )
    # Which gateway produced this day. If it changes mid-series the mileage
    # resolver refuses to diff across it rather than inventing a six-figure day.
    samsara_vehicle_id = models.CharField(max_length=64, blank=True, default="")

    start_odometer_meters = models.DecimalField(
        max_digits=14, decimal_places=1, null=True, blank=True
    )
    end_odometer_meters = models.DecimalField(
        max_digits=14, decimal_places=1, null=True, blank=True
    )
    start_gps_distance_meters = models.DecimalField(
        max_digits=14, decimal_places=1, null=True, blank=True
    )
    end_gps_distance_meters = models.DecimalField(
        max_digits=14, decimal_places=1, null=True, blank=True
    )

    miles_driven = models.DecimalField(
        max_digits=8, decimal_places=1, null=True, blank=True,
        help_text="DERIVED from start/end via dispatching.mileage. "
                  "NULL = unknown (render as em-dash), 0 = provably did not move.",
    )
    mileage_source = models.CharField(
        max_length=8, blank=True, default="",
        help_text="obd (exact) / gps (estimate) / none. Stored so the UI can mark "
                  "provenance and an audit can tell a reading from an estimate.",
    )
    mileage_note = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Why a reading was rejected or fell back. Diagnostics only.",
    )

    sample_count = models.PositiveIntegerField(
        default=0, help_text="Polls that contributed. Low count = sparse day."
    )
    has_gap = models.BooleanField(
        default=False,
        help_text="True when the feed was silent long enough that this day is "
                  "under-counted. Makes a sparse day visibly sparse instead of "
                  "silently wrong.",
    )
    first_sample_at = models.DateTimeField(null=True, blank=True)
    last_sample_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "vehicle__vehicle_number"]
        constraints = [
            # The idempotency key. The nightly upserts on it, so a re-run (or two
            # workers racing) can never produce a second row for the same day.
            models.UniqueConstraint(
                fields=["vehicle", "date"], name="uniq_vehicle_day_reading"
            ),
        ]
        indexes = [models.Index(fields=["date", "vehicle"])]

    def __str__(self):
        miles = "—" if self.miles_driven is None else f"{self.miles_driven} mi"
        return f"{self.vehicle.vehicle_number} {self.date}: {miles}"


class VehicleServiceSchedule(models.Model):
    """
    A recurring maintenance interval for one vehicle — "oil every 5,000 mi or 6
    months, whichever comes first".

    This is the part of fleet management Samsara cannot supply, and the reason
    odometer is worth collecting at all: the odometer is an INPUT, not a
    deliverable. Due-ness is computed in dispatching/fleet_health.py against the
    vehicle's latest odometer, never stored, so it cannot go stale.
    """

    SERVICE_TYPE_CHOICES = [
        ("oil", "Oil change"),
        ("tires", "Tires"),
        ("brakes", "Brakes"),
        ("transmission", "Transmission"),
        ("inspection", "Inspection"),
        ("other", "Other"),
    ]

    vehicle = models.ForeignKey(
        FleetVehicle, on_delete=models.CASCADE, related_name="service_schedules"
    )
    service_type = models.CharField(max_length=32, choices=SERVICE_TYPE_CHOICES)
    interval_miles = models.PositiveIntegerField(
        null=True, blank=True, help_text="Due this many miles after the last one."
    )
    interval_days = models.PositiveIntegerField(
        null=True, blank=True, help_text="Due this many days after the last one."
    )
    last_done_on = models.DateField(null=True, blank=True)
    last_done_odometer_miles = models.DecimalField(
        max_digits=10, decimal_places=1, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["vehicle__vehicle_number", "service_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "service_type"],
                name="uniq_vehicle_service_schedule",
            ),
        ]

    def __str__(self):
        return f"{self.vehicle.vehicle_number} {self.get_service_type_display()}"

    @property
    def due_at_odometer_miles(self):
        """Odometer at which this falls due, or None for a date-only interval."""
        if self.interval_miles is None or self.last_done_odometer_miles is None:
            return None
        return self.last_done_odometer_miles + Decimal(self.interval_miles)

    @property
    def due_on_date(self):
        """Date on which this falls due, or None for a mileage-only interval."""
        if self.interval_days is None or self.last_done_on is None:
            return None
        return self.last_done_on + timedelta(days=self.interval_days)


class VehicleServiceRecord(models.Model):
    """
    A maintenance event that actually happened. Manually logged — Samsara does
    not know what the shop did.

    `out_of_service_from/to` is a LABEL everywhere it appears, never a capacity
    subtraction. Feeding per-unit vehicle state back into dispatch gating was
    built once as Guard A and deliberately removed for firing false positives off
    stale data (dispatching/feasibility_guards.py:140-144). Do not repeat it.
    """

    SERVICE_TYPE_CHOICES = VehicleServiceSchedule.SERVICE_TYPE_CHOICES + [
        ("repair", "Repair"),
    ]

    vehicle = models.ForeignKey(
        FleetVehicle, on_delete=models.PROTECT, related_name="service_records"
    )
    service_type = models.CharField(max_length=32, choices=SERVICE_TYPE_CHOICES)
    performed_on = models.DateField(db_index=True)
    odometer_miles = models.DecimalField(
        max_digits=10, decimal_places=1, null=True, blank=True
    )
    vendor = models.CharField(max_length=120, blank=True)
    cost = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    description = models.TextField(blank=True)

    out_of_service_from = models.DateField(null=True, blank=True)
    out_of_service_to = models.DateField(null=True, blank=True)

    fault_reference = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Free text: the fault code or Samsara issue this addressed.",
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vehicle_service_records",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-performed_on", "-id"]

    def __str__(self):
        return (
            f"{self.vehicle.vehicle_number} {self.get_service_type_display()} "
            f"{self.performed_on}"
        )


class VehicleFault(models.Model):
    """
    An open (or since-resolved) fault EPISODE — not one row per poll.

    A fault seen on 1,000 consecutive polls is one row whose `last_seen_at`
    advances and whose `occurrence_count` increments. The partial unique on
    unresolved rows enforces that, so the sync can upsert blindly.

    Critical rule for the sync: NEVER mass-resolve on a failed API call. An empty
    response because Samsara 500'd is indistinguishable from "all faults cleared"
    unless the caller checks status first — and silently closing every fault is
    exactly the failure that makes people stop trusting the page.
    """

    SOURCE_CHOICES = [
        ("obd_fault", "OBD fault code"),
        ("maintenance", "Samsara maintenance issue"),
        ("dvir", "DVIR defect"),
    ]
    SEVERITY_CHOICES = [
        ("critical", "Critical"),
        ("warning", "Warning"),
        ("info", "Info"),
    ]

    vehicle = models.ForeignKey(
        FleetVehicle, on_delete=models.PROTECT, related_name="faults"
    )
    source = models.CharField(
        max_length=16, choices=SOURCE_CHOICES, default="obd_fault", db_index=True
    )
    external_id = models.CharField(
        max_length=64,
        help_text="Samsara's own id for this fault/issue. The idempotency key — "
                  "never generate our own.",
    )
    code = models.CharField(max_length=32, blank=True, default="")
    severity = models.CharField(
        max_length=16, choices=SEVERITY_CHOICES, blank=True, default=""
    )
    description = models.CharField(max_length=255, blank=True, default="")

    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    occurrence_count = models.PositiveIntegerField(default=1)

    raw = models.JSONField(
        null=True, blank=True,
        help_text="The Samsara payload for this episode. Bounded (one row per "
                  "episode, not per sample) so it stays cheap — unlike a raw "
                  "sample ledger, which would reach millions of rows a year.",
    )

    class Meta:
        ordering = ["-last_seen_at"]
        constraints = [
            # One OPEN episode per (vehicle, source, external_id). Resolved rows
            # are exempt so the same code recurring later opens a fresh episode
            # instead of colliding with the historical one.
            models.UniqueConstraint(
                fields=["vehicle", "source", "external_id"],
                condition=Q(resolved_at__isnull=True),
                name="uniq_open_vehicle_fault",
            ),
        ]
        indexes = [models.Index(fields=["vehicle", "resolved_at"])]

    def __str__(self):
        state = "open" if self.resolved_at is None else "resolved"
        return f"{self.vehicle.vehicle_number} {self.code or self.source} ({state})"

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None


class FleetSyncState(models.Model):
    """
    Health (and, if the delta feed is ever entitled, cursor) for one Samsara feed.
    One row per feed key.

    This exists because of a real ~25-day outage: `.env` defined SAMSARA_API_KEY
    while settings read SAMSARA_API_TOKEN, so the poller no-op'd every cycle and
    every mapped vehicle sat frozen at 2026-07-11 — and nothing in the product
    noticed, because there was no feed-health surface anywhere. The tile that
    renders `last_success_at` is the single highest-value pixel in this module.

    On `cursor`: the /fleet/vehicles/stats endpoint's `pagination.endCursor` is
    INTRA-RESPONSE paging over the vehicle list, not a resume token — persisting
    it would resume a vehicle listing, not a data stream. This field stays empty
    unless and until the cursor-resumable /fleet/vehicles/stats/feed endpoint is
    confirmed entitled (manage.py fleet_probe shows it exists as a route).
    """

    feed = models.CharField(
        max_length=32, unique=True,
        help_text="Feed key: 'vehicle_stats' | 'faults' | 'nightly_reconcile'.",
    )
    cursor = models.TextField(
        blank=True, default="",
        help_text="Resume cursor for a DELTA feed only. Blank for snapshot polls.",
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_status = models.CharField(max_length=16, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["feed"]

    def __str__(self):
        return f"{self.feed}: {self.last_status or 'never run'}"
