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
    exclude_from_timing = models.BooleanField(
        default=False,
        help_text="Exclude this driver's completed trips from route timing data. Affiliates are always excluded."
    )
    night_bonus = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("10.00"),
        help_text="Night pickup bonus (10 PM - 6 AM). Set per driver. $0 for no bonus."
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

    class Meta:
        ordering = ["date", "driver"]

    def save(self, *args, **kwargs):
        # Keep is_available in sync with exception_type so older code paths
        # (admin filters, auto-assigner cascade) keep working.
        self.is_available = self.exception_type != "off"
        super().save(*args, **kwargs)

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

    class Meta:
        unique_together = ("payment", "leg")

    def __str__(self):
        return f"Payment {self.payment.id} - Leg {self.leg.id}"
