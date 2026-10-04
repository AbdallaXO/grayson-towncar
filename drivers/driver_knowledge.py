"""Driver knowledge: what the desk knows about each driver (structured shifts,
Stage 1, S19).

Tags say what a driver is good at (strengths), how they work (habits — some
of them cautions to plan around), the languages they speak and the areas they
know. Each tag on a driver can carry a short note.

This is for people to read. The engine does not use it yet, and it is
staff-only: nothing here may reach a driver-facing page or API. Any staff
user adds tags; only managers (is_superuser) remove one or create a new one —
the views enforce that, not this module.
"""
from django.db import IntegrityError, transaction
from django.db.models import Max

from drivers.models import DriverTag, DriverTagAssignment

#: Categories in the order the profile shows them, with the card's headings.
CATEGORY_LABELS = [
    ("strength", "Strengths"),
    ("habit", "Habits"),
    ("language", "Languages"),
    ("area", "Knows the area"),
]
CATEGORIES = {key for key, _ in DriverTag.CATEGORY_CHOICES}
POLARITIES = {key for key, _ in DriverTag.POLARITY_CHOICES}

TAG_EXISTS = "That tag already exists."


def _tag_key(tag):
    """Within a category: positive tags first, then cautions, each in sort order."""
    return (tag.polarity == "caution", tag.sort_order, tag.name.lower())


def _grouped(items, tag_of):
    """[(label, [item, ...])] in CATEGORY_LABELS order, empty categories left out."""
    by_category = {}
    for item in sorted(items, key=lambda i: _tag_key(tag_of(i))):
        by_category.setdefault(tag_of(item).category, []).append(item)
    return [(label, by_category[key]) for key, label in CATEGORY_LABELS if key in by_category]


def tags_by_category(driver):
    """The driver's tags as [(heading, [DriverTagAssignment, ...])], ordered
    Strengths, Habits, Languages, Knows the area; a category with no tag on
    this driver is left out. One query (tag and added_by come with it)."""
    rows = driver.tag_assignments.select_related("tag", "added_by")
    return _grouped(list(rows), lambda a: a.tag)


def available_tags(driver):
    """The active tags this driver doesn't have yet, grouped the same way, for
    the profile's add picker. One query."""
    tags = DriverTag.objects.filter(is_active=True).exclude(assignments__driver=driver)
    return _grouped(list(tags), lambda t: t)


def add_tag(driver, tag, note, user):
    """Put `tag` on `driver` with an optional note. Idempotent: if the driver
    already has the tag, a new note replaces the old one; a blank note leaves
    the one already there. Returns the DriverTagAssignment."""
    note = (note or "").strip()
    assignment, created = DriverTagAssignment.objects.get_or_create(
        driver=driver, tag=tag, defaults={"note": note, "added_by": user},
    )
    if not created and note and assignment.note != note:
        assignment.note = note
        assignment.save(update_fields=["note"])
    return assignment


def remove_tag(driver, tag):
    """Take `tag` off `driver`. The tag itself stays for everyone else."""
    DriverTagAssignment.objects.filter(driver=driver, tag=tag).delete()


def create_tag(name, category, polarity, user):
    """A new tag, placed after the others in its category. Names are unique
    whatever the capitalisation: a clash raises ValueError(TAG_EXISTS). Spaces
    inside the name are tidied ("  Night   owl " -> "Night owl")."""
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Give the new tag a name.")
    if len(name) > DriverTag._meta.get_field("name").max_length:
        raise ValueError("Keep the tag name under 60 characters.")
    if category not in CATEGORIES:
        raise ValueError("Pick what kind of tag it is.")
    if polarity not in POLARITIES:
        raise ValueError("Pick whether it's a plus or a caution.")
    if DriverTag.objects.filter(name__iexact=name).exists():
        raise ValueError(TAG_EXISTS)
    last = DriverTag.objects.filter(category=category).aggregate(m=Max("sort_order"))["m"]
    try:
        with transaction.atomic():
            return DriverTag.objects.create(
                name=name, category=category, polarity=polarity,
                sort_order=(last or 0) + 1, created_by=user,
            )
    except IntegrityError:                     # same name saved a moment ago
        raise ValueError(TAG_EXISTS) from None
