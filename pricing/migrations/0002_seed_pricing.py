"""
Seed the pricing engine with the launch configuration from the product brief.

Idempotent: every row is get_or_create'd, so re-running on an already-seeded
database (or a prod copy) will not duplicate rows or overwrite numbers an admin
has since edited. The reverse migration is intentionally a no-op so unmigrating
never deletes pricing an admin may have tuned.

All numbers here are the INITIAL values only — they are fully editable at
/admin/pricing/ afterward.
"""

from datetime import date
from decimal import Decimal

from django.db import migrations
from django.utils.text import slugify


# key, display_name, vehicle_type (DB value — unchanged), pax, luggage, ideal_for, sort
VEHICLE_CLASSES = [
    ("towncar", "Towncar", "towncar", 4, 3, "Solo travelers and couples who want a quiet, polished ride.", 1),
    ("mini_van", "Mini Van", "mini_van", 7, 6, "Small families who need room for car seats and bags.", 2),
    ("suv", "SUV", "suv", 6, 6, "Families and execs who want extra space and presence.", 3),
    ("van", "Van", "van", 11, 12, "Larger groups traveling together with luggage.", 4),
    ("sprinter", "Sprinter Van", "Van(14 Pax)", 14, 14, "Big groups and maximum luggage on long hauls.", 5),
]

# key: (hourly_rate, minimum_hours, peak_minimum_hours)
HOURLY = {
    "towncar": (Decimal("115"), Decimal("3"), None),
    "mini_van": (Decimal("120"), Decimal("3"), None),
    "suv": (Decimal("135"), Decimal("3"), None),
    "van": (Decimal("155"), Decimal("3"), None),
    "sprinter": (Decimal("215"), Decimal("3"), Decimal("4")),
}

# key: (base, per_mile, minimum)
FALLBACK = {
    "towncar": (Decimal("145"), Decimal("2.05"), Decimal("250")),
    "mini_van": (Decimal("155"), Decimal("2.30"), Decimal("270")),
    "suv": (Decimal("170"), Decimal("2.65"), Decimal("295")),
    "van": (Decimal("195"), Decimal("2.90"), Decimal("350")),
    "sprinter": (Decimal("310"), Decimal("3.60"), Decimal("525")),
}

# name, approx_miles, aliases, sort, prices keyed by class
# prices order matches the route table: towncar, mini_van, suv, van, sprinter
ROUTES = [
    ("Daytona Beach", 55, "daytona, daytona beach", 1, [250, 270, 295, 350, 525]),
    ("Tampa (TPA)", 84, "tampa, tpa, tampa international, tampa airport", 2, [295, 320, 360, 425, 600]),
    ("St. Petersburg / Clearwater", 108, "st petersburg, saint petersburg, st pete, clearwater", 3, [350, 380, 425, 500, 700]),
    ("Sarasota", 130, "sarasota, srq", 4, [420, 455, 525, 600, 815]),
    ("Jacksonville (JAX)", 140, "jacksonville, jax", 5, [440, 475, 560, 640, 850]),
    ("West Palm Beach (PBI)", 170, "west palm beach, west palm, pbi, palm beach", 6, [525, 565, 700, 800, 1000]),
    ("Naples", 175, "naples", 7, [540, 580, 725, 825, 1050]),
    ("Fort Lauderdale (FLL)", 205, "fort lauderdale, ft lauderdale, fll, lauderdale", 8, [675, 725, 950, 1075, 1200]),
    # NOTE: Miami is intentionally seeded identical to Fort Lauderdale per the
    # product owner ("start with the same price for now"; fully admin-editable).
    ("Miami (MIA)", 235, "miami, mia, miami international, downtown miami", 9, [675, 725, 950, 1075, 1200]),
]

CLASS_ORDER = ["towncar", "mini_van", "suv", "van", "sprinter"]

# label, start, end, multiplier override (None = use global)
PEAK_DATES = [
    ("July 4th Holiday 2026", date(2026, 7, 3), date(2026, 7, 5), None),
    ("Christmas & New Year 2026", date(2026, 12, 20), date(2027, 1, 1), None),
    ("Spring Break 2027", date(2027, 3, 6), date(2027, 3, 21), None),
]


def seed(apps, schema_editor):
    PricingConfig = apps.get_model("pricing", "PricingConfig")
    VehicleClass = apps.get_model("pricing", "VehicleClass")
    HourlyRate = apps.get_model("pricing", "HourlyRate")
    FallbackFormula = apps.get_model("pricing", "FallbackFormula")
    CityRoute = apps.get_model("pricing", "CityRoute")
    CityRoutePrice = apps.get_model("pricing", "CityRoutePrice")
    PeakDate = apps.get_model("pricing", "PeakDate")

    # Singleton config (defaults handle the actual values)
    PricingConfig.objects.get_or_create(id=1)

    classes = {}
    for key, display, vtype, pax, lug, ideal, sort in VEHICLE_CLASSES:
        vc, _ = VehicleClass.objects.get_or_create(
            key=key,
            defaults=dict(
                display_name=display,
                vehicle_type=vtype,
                passenger_capacity=pax,
                luggage_capacity=lug,
                ideal_for=ideal,
                sort_order=sort,
                is_active=True,
            ),
        )
        classes[key] = vc

    for key, (rate, min_h, peak_min) in HOURLY.items():
        HourlyRate.objects.get_or_create(
            vehicle_class=classes[key],
            defaults=dict(hourly_rate=rate, minimum_hours=min_h, peak_minimum_hours=peak_min),
        )

    for key, (base, per_mile, minimum) in FALLBACK.items():
        FallbackFormula.objects.get_or_create(
            vehicle_class=classes[key],
            defaults=dict(base=base, per_mile=per_mile, minimum=minimum),
        )

    for name, miles, aliases, sort, prices in ROUTES:
        route, _ = CityRoute.objects.get_or_create(
            slug=slugify(name)[:100],
            defaults=dict(
                name=name,
                origin_label="Orlando",
                approx_miles=miles,
                aliases=aliases,
                sort_order=sort,
                is_active=True,
            ),
        )
        for cls_key, price in zip(CLASS_ORDER, prices):
            CityRoutePrice.objects.get_or_create(
                city_route=route,
                vehicle_class=classes[cls_key],
                defaults=dict(price=Decimal(str(price))),
            )

    for label, start, end, mult in PEAK_DATES:
        PeakDate.objects.get_or_create(
            label=label,
            defaults=dict(start_date=start, end_date=end, multiplier=mult, is_active=True),
        )


def unseed(apps, schema_editor):
    # No-op: never delete pricing an admin may have tuned.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("pricing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
