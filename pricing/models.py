"""
Admin-editable pricing configuration for the two new service lines:
hourly charters and city-to-city transfers.

Design rule (hard requirement): NO price lives in code, templates, or JS.
Every number a non-engineer might want to change — route fares, the
fallback per-mile formula, hourly rates, minimums, the gratuity %, the peak
multiplier, and the peak-date list — is a row in one of these models, tunable
from the Django admin without a code change or redeploy.

Distance discipline: a quote for a *named* CityRoute reads CityRoutePrice
directly and never touches Google Distance Matrix. Unlisted routes fall back
to RouteDistanceCache (a precomputed/cached miles table); the live Distance
Matrix API is only a last resort, and its result is written back to the cache.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.text import slugify


# ---------------------------------------------------------------------------
# Vehicle classes (pricing-facing)
# ---------------------------------------------------------------------------
class VehicleClass(models.Model):
    """
    A pricing class (Towncar, Mini Van, SUV, Pax Van, Sprinter). This is the
    single key every pricing table hangs off of.

    `vehicle_type` links the class to the existing rates.Vehicle.VEHICLE_TYPES
    value so a quote can be handed off to the booking flow with a real vehicle
    attached. We deliberately do NOT change the rates.Vehicle choices — the
    customer-facing label lives in `display_name` here (e.g. the DB value
    "Van(14 Pax)" is shown as "Sprinter Van").
    """

    # Mirrors rates.Vehicle.VEHICLE_TYPES — kept here as a soft reference so the
    # admin gets a dropdown without importing the rates app at module load.
    VEHICLE_TYPE_CHOICES = [
        ("towncar", "towncar (Towncar)"),
        ("suv", "suv (SUV)"),
        ("mini_van", "mini_van (Mini Van)"),
        ("van", "van (Pax Van)"),
        ("Van(14 Pax)", "Van(14 Pax) (Sprinter Van)"),
    ]

    key = models.SlugField(
        max_length=40,
        unique=True,
        help_text="Stable internal key used by the API and quote payloads "
        "(e.g. 'towncar', 'sprinter'). Do not change once quotes reference it.",
    )
    display_name = models.CharField(
        max_length=60,
        help_text="Customer-facing name shown on the site and quotes "
        "(e.g. 'Sprinter Van', 'Pax Van').",
    )
    vehicle_type = models.CharField(
        max_length=20,
        choices=VEHICLE_TYPE_CHOICES,
        help_text="The rates.Vehicle type this class maps to when a quote "
        "converts into a booking. The DB value is never shown to customers.",
    )
    passenger_capacity = models.PositiveIntegerField(
        default=4, help_text="Shown on the fleet page / quote widget."
    )
    luggage_capacity = models.PositiveIntegerField(default=3)
    ideal_for = models.CharField(
        max_length=200,
        blank=True,
        help_text="One-line 'ideal for…' blurb shown on the fleet card.",
    )
    sort_order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers sort first in dropdowns and grids."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Vehicle class"
        verbose_name_plural = "Vehicle classes"

    def __str__(self):
        return self.display_name


# ---------------------------------------------------------------------------
# Global config (singleton)
# ---------------------------------------------------------------------------
class PricingConfig(models.Model):
    """
    Site-wide pricing knobs. Singleton — there is always exactly one row
    (pk=1). Edit it at /admin/pricing/pricingconfig/.
    """

    gratuity_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("20.00"),
        help_text="Gratuity shown as a separate line on every quote (default 20%).",
    )
    peak_multiplier = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal("1.25"),
        help_text="Default multiplier applied to the base on peak dates "
        "(recommended 1.15–1.50). A PeakDate may override this per event.",
    )
    round_to_whole_dollars = models.BooleanField(
        default=True,
        help_text="Round the final quote (base, gratuity, total) to whole dollars.",
    )
    overtime_increment_minutes = models.PositiveIntegerField(
        default=30,
        help_text="Hourly overtime past the minimum is billed in increments of "
        "this many minutes.",
    )

    # Editable display copy (so marketing strings aren't hardcoded either)
    all_inclusive_note = models.CharField(
        max_length=120,
        default="All-inclusive — tolls included.",
        help_text="Shown on city-to-city quotes.",
    )
    hourly_tolls_note = models.CharField(
        max_length=120,
        default="Tolls & parking billed at cost.",
        help_text="Shown on hourly quotes.",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Pricing configuration"
        verbose_name_plural = "Pricing configuration"

    def __str__(self):
        return "Pricing configuration"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "PricingConfig":
        """Return the singleton, creating it with defaults if missing."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ---------------------------------------------------------------------------
# Hourly charters
# ---------------------------------------------------------------------------
class HourlyRate(models.Model):
    """Per-class hourly charter rate and minimum hours."""

    vehicle_class = models.OneToOneField(
        VehicleClass, on_delete=models.CASCADE, related_name="hourly_rate"
    )
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2)
    minimum_hours = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=Decimal("3.0"),
        help_text="Minimum billable hours (default 3).",
    )
    peak_minimum_hours = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="Optional higher minimum on peak dates (e.g. Sprinter = 4 hr "
        "on peak). Leave blank to use the normal minimum on peak dates too.",
    )

    class Meta:
        ordering = ["vehicle_class__sort_order"]

    def __str__(self):
        return f"{self.vehicle_class.display_name} — ${self.hourly_rate}/hr"


# ---------------------------------------------------------------------------
# City-to-city: fallback per-mile formula
# ---------------------------------------------------------------------------
class FallbackFormula(models.Model):
    """
    Per-class formula used for any city-to-city route NOT in the route table:
        chargeable_miles = loaded_miles * deadhead_factor
        price            = base + chargeable_miles * per_mile
        price            = max(price, minimum)   # floor

    `deadhead_factor` covers the empty return trip our owned vehicles make after
    a one-way run (the car is based in the Orlando service area and must drive
    back). It inflates ONLY the mileage component, never the named-route flat
    prices (those already bake in the return).
    """

    vehicle_class = models.OneToOneField(
        VehicleClass, on_delete=models.CASCADE, related_name="fallback_formula"
    )
    base = models.DecimalField(max_digits=8, decimal_places=2)
    per_mile = models.DecimalField(max_digits=6, decimal_places=2)
    deadhead_factor = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal("1.80"),
        help_text="Multiplier on the loaded miles to cover the empty return "
        "trip (1.0 = charge loaded miles only, 2.0 = full round-trip). "
        "Recommended ~1.8. Applies to this per-mile formula only, never to "
        "named-route flat prices. The customer still sees the true loaded miles.",
    )
    minimum = models.DecimalField(
        max_digits=8, decimal_places=2, help_text="Price floor for this class."
    )

    class Meta:
        ordering = ["vehicle_class__sort_order"]

    def __str__(self):
        return (
            f"{self.vehicle_class.display_name} — "
            f"${self.base} + ${self.per_mile}/mi ×{self.deadhead_factor} "
            f"(min ${self.minimum})"
        )


# ---------------------------------------------------------------------------
# City-to-city: named route table (authoritative; zero distance lookups)
# ---------------------------------------------------------------------------
class CityRoute(models.Model):
    """
    A named one-way city-to-city route (e.g. Orlando → Miami). When a quote's
    (origin, destination) matches an active route, CityRoutePrice is
    authoritative and NO distance lookup happens.

    `aliases` lets free-text destinations resolve to this route (the widget
    normally sends the route id directly from the dropdown, but the API also
    matches typed destinations against the name + aliases).
    """

    name = models.CharField(
        max_length=80, help_text="Destination label, e.g. 'Miami (MIA)'."
    )
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    origin_label = models.CharField(
        max_length=80,
        default="Orlando",
        help_text="Origin shown to the customer (routes are priced one-way "
        "from the Orlando service area).",
    )
    approx_miles = models.PositiveIntegerField(
        default=0, help_text="Approximate one-way miles (informational only)."
    )
    aliases = models.TextField(
        blank=True,
        help_text="Comma-separated alternate names a customer might type "
        "(e.g. 'MIA, Miami International, Downtown Miami').",
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:100]
        super().save(*args, **kwargs)

    def alias_list(self) -> list[str]:
        terms = [self.name] + [a.strip() for a in self.aliases.split(",")]
        return [t.lower() for t in terms if t and t.strip()]


class CityRoutePrice(models.Model):
    """One-way, all-inclusive (tolls included), pre-gratuity price for a
    (CityRoute, VehicleClass) pair."""

    city_route = models.ForeignKey(
        CityRoute, on_delete=models.CASCADE, related_name="prices"
    )
    vehicle_class = models.ForeignKey(
        VehicleClass, on_delete=models.CASCADE, related_name="route_prices"
    )
    price = models.DecimalField(
        max_digits=9,
        decimal_places=2,
        help_text="One-way price (USD), tolls included, before gratuity.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["city_route", "vehicle_class"],
                name="unique_route_class_price",
            )
        ]
        ordering = ["city_route__sort_order", "vehicle_class__sort_order"]

    def __str__(self):
        return f"{self.city_route.name} / {self.vehicle_class.display_name}: ${self.price}"


# ---------------------------------------------------------------------------
# Peak dates
# ---------------------------------------------------------------------------
class PeakDate(models.Model):
    """
    An inclusive date range on which the peak multiplier applies (Christmas
    week, spring break, July 4, OCCC closeout dates, etc.). Fully editable.
    """

    label = models.CharField(max_length=120)
    start_date = models.DateField()
    end_date = models.DateField(help_text="Inclusive — the last peak day.")
    multiplier = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Optional override of the global peak multiplier for this "
        "event. Leave blank to use the global value.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["start_date"]
        verbose_name = "Peak date"
        verbose_name_plural = "Peak dates"

    def __str__(self):
        return f"{self.label} ({self.start_date} → {self.end_date})"

    def covers(self, d) -> bool:
        return self.is_active and self.start_date <= d <= self.end_date


# ---------------------------------------------------------------------------
# Distance cache (for unlisted routes only)
# ---------------------------------------------------------------------------
class RouteDistanceCache(models.Model):
    """
    Cached one-way driving miles for an (origin, destination) text pair, so the
    fallback formula path never re-calls Google Distance Matrix for a pair we
    have already resolved. Named CityRoutes never reach this table.
    """

    SOURCE_CHOICES = [
        ("manual", "Manually entered"),
        ("distance_matrix", "Google Distance Matrix"),
        ("seed", "Seeded"),
    ]

    origin_key = models.CharField(max_length=160, db_index=True)
    destination_key = models.CharField(max_length=160, db_index=True)
    origin_text = models.CharField(max_length=200)
    destination_text = models.CharField(max_length=200)
    miles = models.DecimalField(max_digits=7, decimal_places=2)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["origin_key", "destination_key"],
                name="unique_distance_pair",
            )
        ]
        verbose_name = "Route distance (cache)"
        verbose_name_plural = "Route distances (cache)"

    def __str__(self):
        return f"{self.origin_text} → {self.destination_text}: {self.miles} mi"


# ---------------------------------------------------------------------------
# Instant quote (the hand-off token)
# ---------------------------------------------------------------------------
class InstantQuote(models.Model):
    """
    A computed quote, persisted so the booking flow can convert it into a
    reservation without the customer re-entering the trip. The API returns this
    record's `token`; the Reserve button links to /book-quote/<token>/.
    """

    SERVICE_CHOICES = [
        ("hourly", "Hourly charter"),
        ("city_to_city", "City-to-city transfer"),
    ]
    SOURCE_CHOICES = [
        ("route_table", "Named route table"),
        ("formula", "Per-mile formula"),
        ("hourly", "Hourly rate"),
    ]

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    service_type = models.CharField(max_length=20, choices=SERVICE_CHOICES)
    vehicle_class = models.ForeignKey(
        VehicleClass, on_delete=models.PROTECT, related_name="quotes"
    )
    service_date = models.DateField()

    # City-to-city
    origin = models.CharField(max_length=200, blank=True)
    destination = models.CharField(max_length=200, blank=True)
    city_route = models.ForeignKey(
        CityRoute,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quotes",
    )
    loaded_miles = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True
    )

    # Hourly
    hours = models.DecimalField(
        max_digits=5, decimal_places=1, null=True, blank=True
    )

    # Computed amounts (stored so the booking can't drift from the quote)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    peak_adjustment = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    gratuity = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    all_inclusive = models.BooleanField(default=False)
    price_source = models.CharField(max_length=20, choices=SOURCE_CHOICES)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    converted_reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_quote",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Instant quote"
        verbose_name_plural = "Instant quotes"

    def __str__(self):
        return f"{self.get_service_type_display()} — {self.vehicle_class.display_name} — ${self.total}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def effective_base(self) -> Decimal:
        """Base including any peak adjustment — the figure gratuity was taken
        on, and the value used as the reservation's base_price."""
        return self.base_price + (self.peak_adjustment or Decimal("0"))

    def trip_label(self) -> str:
        if self.service_type == "city_to_city":
            return f"{self.origin} → {self.destination}"
        return f"{self.hours}-hour charter"
