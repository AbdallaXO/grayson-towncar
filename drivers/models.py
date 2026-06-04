from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from reservations.models import Leg
from decimal import Decimal


class Driver(models.Model):
    DRIVER_TYPE_CHOICES = [
        ("inhouse", "Inhouse"),
        ("affiliate", "Affiliate/Outhouse"),
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
    vehicle_number = models.CharField(max_length=50, unique=True)
    vehicle_type = models.ForeignKey(
        "rates.Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Vehicle type/tier for scheduling (e.g. SUV, Van, MiniVan)"
    )
    year = models.PositiveIntegerField()
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["vehicle_number"]

    def __str__(self):
        return f"{self.vehicle_number} - {self.year} {self.make} {self.model}"


class DriverVehicleAssignment(models.Model):
    driver = models.ForeignKey(
        "Driver", on_delete=models.CASCADE, related_name="vehicle_assignments"
    )
    date = models.DateField()
    vehicle = models.ForeignKey(
        "FleetVehicle", on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta:
        unique_together = ("driver", "date")
        ordering = ["date", "driver"]

    def __str__(self):
        return f"{self.driver} - {self.vehicle} ({self.date})"


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
