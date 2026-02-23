"""Expand Location aliases for better route matching and remove unused duplicate locations.

Universal: Added Sapphire Falls, Hard Rock Hotel, Helios Grand, Aventura Hotel,
           Stella Nova, Terra Luna, Cabana Bay, Endless Summer, Dockside/Surfside Inn
Disney:    Added Lake Buena Vista, Bay Lake Tower, Wilderness Lodge, BoardWalk,
           Yacht Club, Beach Club, Pop Century, and 10+ more resort-specific aliases
Port Canaveral: Added Cape Canaveral, Cocoa Beach, Cocoa, cruise line names
                (Carnival, Royal Caribbean, Disney Cruise, Norwegian, MSC, etc.)

Also removes duplicate Location entries (IDs 11-30) that had no aliases, no routes,
and no legs referencing them.
"""
from django.db import migrations


UPDATED_ALIASES = {
    2: (
        "Universal, Portofino Bay, Royal Pacific, Sapphire Falls, "
        "Hard Rock Hotel, Helios Grand, Aventura Hotel, Stella Nova, "
        "Terra Luna, Cabana Bay, Endless Summer, Dockside Inn, Surfside Inn"
    ),
    3: (
        "Disney, Walt Disney World, Art of Animation, Bonnet Creek, "
        "Saratoga Springs, Lake Buena Vista, Bay Lake Tower, Wilderness Lodge, "
        "BoardWalk, Yacht Club, Beach Club, Pop Century, Caribbean Beach, "
        "Coronado Springs, Polynesian, Contemporary Resort, Animal Kingdom Lodge, "
        "All-Star, Port Orleans, Old Key West, Riviera Resort, Fort Wilderness, "
        "Grand Floridian"
    ),
    8: (
        "Cape Canaveral, Cocoa Beach, Cocoa, Carnival, Royal Caribbean, "
        "Disney Cruise, Norwegian Cruise, MSC Cruises, Princess Cruises, "
        "Celebrity Cruises, Cruise Terminal, Cruise Port"
    ),
}

# Previous aliases (for reverse migration)
PREVIOUS_ALIASES = {
    2: "Universal, Portofino Bay, Portfino Bay, Royal Pacific",
    3: "Disney, Walt Disney World, Art of Animation, Bonnet Creek, Saratoga Springs",
    8: "",
}

# Duplicate location IDs with no aliases, routes, or legs
DUPLICATE_IDS = list(range(11, 31))


def update_aliases_and_cleanup(apps, schema_editor):
    Location = apps.get_model("rates", "Location")

    # Update aliases
    for loc_id, aliases in UPDATED_ALIASES.items():
        Location.objects.filter(id=loc_id).update(aliases=aliases)

    # Remove unused duplicates
    Location.objects.filter(id__in=DUPLICATE_IDS).delete()


def reverse_aliases(apps, schema_editor):
    Location = apps.get_model("rates", "Location")
    for loc_id, aliases in PREVIOUS_ALIASES.items():
        Location.objects.filter(id=loc_id).update(aliases=aliases)
    # Note: duplicate locations are not recreated on reverse


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0018_populate_location_aliases"),
    ]

    operations = [
        migrations.RunPython(update_aliases_and_cleanup, reverse_aliases),
    ]
