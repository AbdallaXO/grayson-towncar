"""Set up the pay zones, and record the local places the matcher didn't know.

Sixteen of the nineteen existing Route rows already follow one rule: a trip
between two local places pays $25, a trip touching Sanford or Port Canaveral
pays $40, and Sanford to the port pays $50. Only Championsgate ($35) and
Flamingo Crossings ($30) genuinely differ, and those keep their Route rows,
which override the zone.

The pairs nobody ever entered are why ~150 legs a month were priced off
whatever rate the customer happened to book. With zones there is nothing to
miss: any two placed endpoints price, whether or not that exact pairing exists.

Zones are rows, not code. New ones can be added in the admin and priced against
the existing ones without a deploy.

Aliases: every property listed below was in fact paid the local rate whenever a
person looked at it, so these record existing practice rather than setting new
prices.

Deliberately left with NO zone, so trips there stay unpriced and get flagged
rather than guessed at:
  * a bare "Hilton Orlando" — three different hotels share that name, and two of
    them already resolve correctly through longer aliases
  * Kia Center — downtown, no zone agreed
  * private residential addresses, which can never be aliased

Idempotent: re-running creates no duplicate zones, rates or aliases.
"""
from decimal import Decimal

from django.db import migrations

LOCAL = "Local"
OUTER = "Outer — Sanford & Port Canaveral"
TAMPA = "Tampa"

ZONE_DEFS = [
    (LOCAL, 10, "The airport, Disney, Universal, I-Drive, the theme parks, and "
                "hotel-to-hotel transfers around them. The everyday run."),
    (OUTER, 20, "Sanford airport and Port Canaveral. Further out, longer drive, "
                "higher rate."),
    (TAMPA, 30, "Tampa. Out of area — a long haul, priced on its own."),
]

# Unordered pairs. A pair with no row here is NOT priced: those trips are
# flagged on the driver pay page instead. Tampa is only priced from Local
# because that is the only Tampa run anyone has quoted.
ZONE_RATES = [
    (LOCAL, LOCAL, Decimal("25.00")),
    (LOCAL, OUTER, Decimal("40.00")),
    (OUTER, OUTER, Decimal("50.00")),
    (LOCAL, TAMPA, Decimal("100.00")),
]

LOCATION_ZONES = {
    "Orlando International Airport": LOCAL,
    "Universal Studios Area Hotels": LOCAL,
    "All WDW Disney Property Resorts": LOCAL,
    "International Drive Hotels": LOCAL,
    "Disney Springs Hotels": LOCAL,
    "Kissimmee 192 Area Hotels": LOCAL,
    "Omni Championsgate / Reunion": LOCAL,
    "Sea World": LOCAL,
    "Winter Garden Resorts": LOCAL,
    "Flamingo Crossings": LOCAL,
    "Port Canaveral": OUTER,
    "Sanford Int'l Airport": OUTER,
}

# Tampa had no Location at all, which is why those runs fell through to the
# customer's booking rate. Historically paid $100–$125.
NEW_LOCATIONS = [
    ("Tampa International Airport", TAMPA, "Tampa Airport, TPA, Tampa Intl"),
]

# Local properties the address matcher did not recognise. Counts are completed
# in-house legs in the snapshot; all were paid the local rate.
NEW_ALIASES = {
    "All WDW Disney Property Resorts": [
        "Shades of Green",        # 187 legs
        "Magic Kingdom",          # 45 legs — covers "Magic Kingdom Park", "- TTC"
    ],
    "Universal Studios Area Hotels": [
        "Epic Universe",          # 108 legs — covers "Universal Epic Universe"
    ],
    "Orlando International Airport": [
        "Brightline",             # 47 legs — the station is inside MCO Terminal C
    ],
    "Disney Springs Hotels": [
        "Evermore",               # 102 legs — covers "Conrad Orlando at Evermore"
        "World Center Marriott",  # 29 legs
        "Grand Cypress",          # 20 legs
        "Caribe Royale",          # 15 legs
        "Tuscany Village",        # 10 legs
    ],
    "Kissimmee 192 Area Hotels": [
        "Margaritaville",         # 42 legs
        "Gaylord Palms",          # 27 legs
    ],
}


def _alias_list(location):
    return [a.strip() for a in (location.aliases or "").split(",") if a.strip()]


def seed(apps, schema_editor):
    Location = apps.get_model("rates", "Location")
    Zone = apps.get_model("rates", "Zone")
    ZoneRate = apps.get_model("rates", "ZoneRate")

    zones = {}
    for name, order, desc in ZONE_DEFS:
        zone, _ = Zone.objects.get_or_create(
            name=name, defaults={"sort_order": order, "description": desc}
        )
        zones[name] = zone

    for a, b, pay in ZONE_RATES:
        za, zb = zones[a], zones[b]
        if za.id > zb.id:
            za, zb = zb, za
        ZoneRate.objects.update_or_create(
            zone_a=za, zone_b=zb, defaults={"inhouse_base_pay": pay}
        )

    for name, zone_name in LOCATION_ZONES.items():
        Location.objects.filter(name=name).update(pay_zone=zones[zone_name])

    for name, zone_name, aliases in NEW_LOCATIONS:
        Location.objects.get_or_create(
            name=name, defaults={"pay_zone": zones[zone_name], "aliases": aliases}
        )

    for name, additions in NEW_ALIASES.items():
        loc = Location.objects.filter(name=name).first()
        if loc is None:
            continue
        existing = _alias_list(loc)
        lowered = {a.lower() for a in existing}
        added = [a for a in additions if a.lower() not in lowered]
        if added:
            loc.aliases = ", ".join(existing + added)
            loc.save(update_fields=["aliases"])


def unseed(apps, schema_editor):
    Location = apps.get_model("rates", "Location")
    Zone = apps.get_model("rates", "Zone")

    Location.objects.filter(name__in=[n for n, _, _ in NEW_LOCATIONS]).delete()
    Location.objects.filter(pay_zone__isnull=False).update(pay_zone=None)
    # ZoneRate rows cascade with their zones.
    Zone.objects.filter(name__in=[n for n, _, _ in ZONE_DEFS]).delete()

    for name, additions in NEW_ALIASES.items():
        loc = Location.objects.filter(name=name).first()
        if loc is None:
            continue
        lowered = {a.lower() for a in additions}
        kept = [a for a in _alias_list(loc) if a.lower() not in lowered]
        loc.aliases = ", ".join(kept)
        loc.save(update_fields=["aliases"])


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0025_zone_location_pay_zone_zonerate"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
