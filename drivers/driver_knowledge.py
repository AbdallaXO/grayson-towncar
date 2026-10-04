"""Driver knowledge: what the desk knows about each driver (structured shifts,
Stage 1, S19).

Tags say what a driver is good at (strengths), how they work (habits — some
of them cautions to plan around), the languages they speak and the areas they
know. Each tag on a driver can carry a short note.

The log is a dated record of compliments, complaints, incidents and notes,
each optionally tied to a trip. A complaint or an incident can be a strike;
strikes count for the last 12 months (STRIKE_WINDOW_DAYS).

This is for people to read. The engine does not use it yet, and it is
staff-only: nothing here may reach a driver-facing page or API. Any staff
user adds tags and log entries; only managers (is_superuser) remove a tag,
create one, mark a strike, or edit or delete an entry — the views and the
log form enforce that, not this module.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Max

from business.datefmt import strf
from drivers.models import STRIKE_WINDOW_DAYS, DriverLogEntry, DriverTag, DriverTagAssignment
from drivers.operator_jobs import short_place
from reservations.models import Leg

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
TAG_SWITCHED_OFF = ("That tag already exists but is switched off. Turn it back on "
                    "under Driver tags in the Django admin.")


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
    whatever the capitalisation: a clash raises ValueError(TAG_EXISTS), or
    ValueError(TAG_SWITCHED_OFF) when the tag it clashes with is switched off.
    Spaces inside the name are tidied ("  Night   owl " -> "Night owl")."""
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Give the new tag a name.")
    name_max = DriverTag._meta.get_field("name").max_length
    if len(name) > name_max:
        raise ValueError(f"Keep the tag name to {name_max} characters or fewer.")
    if category not in CATEGORIES:
        raise ValueError("Pick what kind of tag it is.")
    if polarity not in POLARITIES:
        raise ValueError("Pick whether it's a plus or a caution.")
    clash = DriverTag.objects.filter(name__iexact=name).first()
    if clash is not None:
        raise ValueError(TAG_EXISTS if clash.is_active else TAG_SWITCHED_OFF)
    last = DriverTag.objects.filter(category=category).aggregate(m=Max("sort_order"))["m"]
    try:
        with transaction.atomic():
            return DriverTag.objects.create(
                name=name, category=category, polarity=polarity,
                sort_order=(last or 0) + 1, created_by=user,
            )
    except IntegrityError:          # same name, any case, saved a moment ago
        raise ValueError(TAG_EXISTS) from None


# ── The log ─────────────────────────────────────────────────────────────────

LOG_KINDS = {key for key, _ in DriverLogEntry.KIND_CHOICES}
#: The log card's filter links, as (?log= value, label); "" is everything.
LOG_FILTERS = [
    ("", "All"),
    ("compliment", "Compliments"),
    ("complaint", "Complaints"),
    ("incident", "Incidents"),
    ("note", "Notes"),
]
#: How far back the log form's trip picker reaches.
RECENT_TRIP_DAYS = 60


def log_filter_href(path, query, kind="", edit_mode=False):
    """The profile's address with only ?log= changed (dropped for "", which is
    everything), so whatever else is on it, like the guest-texting window
    (?comms=), stays put. ?edit=1 is kept in edit mode and dropped otherwise.
    A relative "?…" when anything is left; otherwise `path` itself."""
    params = query.copy()
    params.pop("log", None)
    if edit_mode:
        params["edit"] = "1"
    else:
        params.pop("edit", None)
    if kind:
        params["log"] = kind
    qs = params.urlencode()
    return f"?{qs}" if qs else path


def log_filter_links(path, query, edit_mode=False):
    """The log card's filter links, as (?log= value, label, href)."""
    return [(key, label, log_filter_href(path, query, key, edit_mode))
            for key, label in LOG_FILTERS]


def log_entries(driver, kind=None):
    """The driver's log, newest first (by the day it happened, then by when it
    was logged). `kind` narrows it to one kind; anything else means all.
    The trip, its reservation and who logged or edited it come with each row."""
    entries = driver.log_entries.select_related(
        "leg__reservation", "logged_by", "updated_by",
    )
    if kind in LOG_KINDS:
        entries = entries.filter(kind=kind)
    return entries


def strike_count(driver, today):
    """Strikes that happened within the last STRIKE_WINDOW_DAYS: on a day after
    `today` minus 365 days. A strike exactly a year old no longer counts."""
    since = today - timedelta(days=STRIKE_WINDOW_DAYS)
    return driver.log_entries.filter(is_strike=True, occurred_on__gt=since).count()


def recent_legs_for(driver, today, days=RECENT_TRIP_DAYS):
    """The driver's trips from the last `days` days up to and including today,
    newest first, leaving out cancelled legs and cancelled reservations (both
    spellings). For the log form's trip picker."""
    return (Leg.objects
            .filter(driver=driver, pickup_date__gte=today - timedelta(days=days),
                    pickup_date__lte=today)
            .exclude(status="cancelled")
            .exclude(reservation__status__in=("cancelled", "canceled"))
            .order_by("-pickup_date", "-pickup_time", "-id"))


def trip_label(leg):
    """"Oct 3 · 5:00 AM · MCO → Disney's Polynesian Village Resort"."""
    return " · ".join([
        strf(leg.pickup_date, "%b %-d"),
        strf(leg.pickup_time, "%-I:%M %p"),
        f"{short_place(leg.pickup_location)} → {short_place(leg.dropoff_location)}",
    ])
