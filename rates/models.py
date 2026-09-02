from django.db import models
from django.utils.text import slugify


class Vehicle(models.Model):
    """
    Specifies details about a vehicle, such as its type, passenger capacity,
    and luggage capacity. Can be related to a Reservation or used for pricing via Rate.
    """

    VEHICLE_TYPES = [
        ("towncar", "Towncar"),
        ("suv", "SUV"),
        ("mini_van", "Mini Van"),
        ("van", "Van"),
        ("Van(14 Pax)", "Van (14 Pax)"),
    ]

    vehicle_type = models.CharField(max_length=20, choices=VEHICLE_TYPES)
    capacity = models.PositiveIntegerField()
    luggage_capacity = models.PositiveIntegerField()
    requires_certification = models.BooleanField(
        default=False,
        help_text="If checked, only drivers explicitly cleared for this vehicle type may be "
                  "assigned it (e.g. the Sprinter / 14-passenger van). Assigning it to an "
                  "uncertified driver is blocked.",
    )
    image = models.ImageField(upload_to="vehicles/", blank=True)
    ff_carseats_max = models.PositiveIntegerField(
        default=2,
        help_text="Max forward-facing car seats that physically fit",
    )
    rf_carseats_max = models.PositiveIntegerField(
        default=2,
        help_text="Max rear-facing car seats that physically fit",
    )
    boosters_max = models.PositiveIntegerField(
        default=2,
        help_text="Max booster seats that physically fit",
    )
    carseats_capacity = models.PositiveIntegerField(
        default=4,
        help_text="Total child seats (FF + RF + boosters) that physically fit",
    )
    included_carseats = models.PositiveIntegerField(
        default=2,
        help_text="Car seats (FF + RF combined) included in base price — extras cost the fee below",
    )
    included_boosters = models.PositiveIntegerField(
        default=2,
        help_text="Booster seats included in base price — extras cost the fee below",
    )
    carseats_display = models.CharField(max_length=100, blank=True)
    extra_carseat_fee = models.DecimalField(
        max_digits=6, decimal_places=2, default=30,
        help_text="Fee per additional car seat beyond included limit",
    )
    extra_booster_fee = models.DecimalField(
        max_digits=6, decimal_places=2, default=30,
        help_text="Fee per additional booster seat beyond included limit",
    )
    extra_stop_fee = models.DecimalField(
        max_digits=6, decimal_places=2, default=0,
        help_text="Flat fee per intermediate stop (same-area drop-offs, luggage drops, etc.)",
    )

    def __str__(self):
        """
        Returns the vehicle type in uppercase (e.g., 'SUV', 'VAN').
        """
        return str(self.vehicle_type.title())

    class Meta:
        ordering = ["capacity"]


class LocationGroup(models.Model):
    """
    A geographic / operational grouping of Locations (e.g., 'Disney Area',
    'Universal Area', 'Cruise Port Area'). Used to gate which extra stops
    a customer may add to a given route. Empty/null group on Location means
    the location is not eligible to be offered as an automated extra stop —
    it can still be used as a primary pickup/dropoff.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Zone(models.Model):
    """A driver-pay zone — a PRICE TIER, not a place.

    Every trip between two Local places pays the same, whichever two they are.
    That is how the rate table already behaved: 16 of the 19 hand-built Route
    rows fall out of this rule exactly. Routes remain the override for the pairs
    that genuinely differ (Championsgate, Flamingo Crossings).

    Zones are rows, not code, so new ones (a Tampa run, a Miami run) can be
    added and priced without a developer.
    """

    name = models.CharField(max_length=60, unique=True)
    description = models.TextField(
        blank=True,
        help_text="What belongs in this zone, in your own words. Shown nowhere "
                  "but the admin — it is for whoever picks the zone next.",
    )
    sort_order = models.PositiveSmallIntegerField(
        default=100,
        help_text="Display order. Lower comes first.",
    )

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Location(models.Model):
    name = models.CharField(max_length=70)
    aliases = models.TextField(
        blank=True,
        help_text="Comma-separated alternate names (e.g., MCO, Orlando Airport, Disney)",
    )
    pay_zone = models.ForeignKey(
        "Zone",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locations",
        help_text="Which pay zone this place is in. Leave blank for somewhere with no "
                  "agreed price yet: trips there stay unpriced and are flagged on the "
                  "driver pay page instead of being guessed at.",
    )
    group = models.ForeignKey(
        "LocationGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locations",
        help_text="Optional area grouping. Locations sharing a group can be offered as extra stops on routes that allow that group.",
    )

    def __str__(self):
        return self.name


class ZoneRate(models.Model):
    """What a driver is paid for a trip between two pay zones.

    This is the default that applies when no explicit Route row exists for the
    pair — which is most pairs, because the Route table only ever enumerated
    the handful anyone got round to entering. Everything else used to fall back
    to the price on the customer's booking, which produced confident wrong
    numbers (a Clermont run to the cruise port paid as an airport transfer).

    Zone pairs are unordered: (1, 2) and (2, 1) are the same trip, so the rows
    are stored with the lower zone first and looked up the same way.
    """

    zone_a = models.ForeignKey(
        "Zone", on_delete=models.CASCADE, related_name="rates_as_a"
    )
    zone_b = models.ForeignKey(
        "Zone", on_delete=models.CASCADE, related_name="rates_as_b"
    )
    inhouse_base_pay = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Base pay for an in-house driver on any trip between these two zones.",
    )

    class Meta:
        unique_together = (("zone_a", "zone_b"),)
        ordering = ["zone_a__sort_order", "zone_b__sort_order"]
        verbose_name = "Zone rate"

    def save(self, *args, **kwargs):
        # Stored with the lower id first so (A,B) and (B,A) cannot both exist.
        if self.zone_a_id and self.zone_b_id and self.zone_a_id > self.zone_b_id:
            self.zone_a_id, self.zone_b_id = self.zone_b_id, self.zone_a_id
        super().save(*args, **kwargs)

    @classmethod
    def pay_for(cls, zone_a_id, zone_b_id):
        """Base pay for a trip between two zones, or None if either has no zone
        or nobody has priced that pairing yet."""
        if not zone_a_id or not zone_b_id:
            return None
        lo, hi = sorted((zone_a_id, zone_b_id))
        row = cls.objects.filter(zone_a_id=lo, zone_b_id=hi).first()
        return row.inhouse_base_pay if row else None

    def __str__(self):
        return f"{self.zone_a} ↔ {self.zone_b}: ${self.inhouse_base_pay}"


class Route(models.Model):
    """
    Defines a specific route (e.g., 'Disney Property <--> MCO').

    A Route is an OVERRIDE on top of the zone rate: it exists for pairs that
    genuinely price differently from their zone (Championsgate, Flamingo
    Crossings). Pairs that follow the zone rule need no row at all.
    """

    origin = models.ForeignKey(
        "Location", related_name="origin", on_delete=models.CASCADE
    )
    destination = models.ForeignKey(
        "Location", related_name="destination", on_delete=models.CASCADE
    )
    slug = models.SlugField(max_length=200, blank=True, null=True)
    description = models.TextField(blank=True)
    inhouse_base_pay = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Default base pay for inhouse drivers on this route",
    )
    allowed_extra_stop_groups = models.ManyToManyField(
        "LocationGroup",
        blank=True,
        related_name="routes_allowing_extra_stops",
        help_text="LocationGroups whose locations may be offered to customers as add-on stops on this route. Empty = no extra-stop picker shown.",
    )

    class Meta:
        unique_together = (
            "origin",
            "destination",
        )
        # Ensure unique origin-destination pairs

    def save(self, *args, **kwargs):
        """
        Automatically generates a slug based on the route name if not provided.
        """
        if not self.slug:
            self.slug = slugify(f"{self.origin} to {self.destination}")
        super().save(*args, **kwargs)

    def __str__(self):
        """
        Returns the route name.
        """
        return f"{self.origin.name} ⇄ {self.destination.name}"


from django.db import models
from django.db.models import UniqueConstraint


class Rate(models.Model):
    """
    Associates a specific Vehicle with a specific Route, defining
    one-way and round-trip prices for that combination.
    """

    vehicle = models.ForeignKey(
        "Vehicle",
        on_delete=models.CASCADE,
        related_name="rates",
        db_index=True,  # Additional index on this foreign key
    )
    route = models.ForeignKey(
        "Route",
        on_delete=models.CASCADE,
        db_index=True,  # Additional index on this foreign key
    )
    oneway_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        db_index=True,  # Optional: if you frequently filter by price
    )
    round_trip_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        db_index=True,  # Optional: if you frequently filter by price
    )

    class Meta:
        # Use UniqueConstraint for more flexibility
        constraints = [
            UniqueConstraint(fields=["vehicle", "route"], name="unique_vehicle_route")
        ]

        # Multiple indexes for different query patterns
        indexes = [
            # Composite index for vehicle and route
            models.Index(fields=["vehicle"]),
            # Optional: additional indexes for common query patterns
            models.Index(fields=["oneway_price"]),
            models.Index(fields=["round_trip_price"]),
        ]

        # Custom ordering to prioritize Orlando Airport first
        ordering = [
            # First, prioritize Orlando International Airport
            models.Case(
                models.When(
                    route__origin__name="Orlando International Airport", then=0
                ),
                default=1,
            ),
            # Then order by origin name, then destination name
            "route__origin__name",
            "route__destination__name",
        ]

    def __str__(self):
        """
        Returns a combination of Vehicle and Route for pricing details.
        """
        return f"{self.vehicle} - {self.route}"
