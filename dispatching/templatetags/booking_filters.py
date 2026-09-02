"""Template helpers for the dispatcher booking wizard.

The wizard keeps its in-progress trip in the session, where every value is a
string ("2026-09-05", "10:30:00").  The summary rail and the review step need
to print those as real dates and times, so they are parsed back here rather
than in five separate views.
"""

from datetime import date, datetime, time

from django import template

register = template.Library()


@register.filter
def iso_date(value):
    """'2026-09-05' -> date(2026, 9, 5). Passes real dates through."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


@register.filter
def iso_time(value):
    """'10:30:00' or '10:30' -> time(10, 30). Passes real times through."""
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value).strip(), fmt).time()
        except (ValueError, TypeError):
            continue
    return None


@register.filter
def days_from_today(value):
    """Whole days between today and an ISO date string. None when unparseable."""
    d = iso_date(value)
    if d is None:
        return None
    from django.utils import timezone
    return (d - timezone.localdate()).days


@register.filter
def short_place(value):
    """Trim the long MCO label down for tight rows."""
    if not value:
        return ""
    return str(value).replace("Orlando International Airport (MCO)", "MCO")


@register.filter
def indef_article(name):
    """'Towncar' -> 'a Towncar'; 'SUV' -> 'an SUV'. For the read-back sentence."""
    if not name:
        return ""
    text = str(name).strip()
    return ("an " if text[0].upper() in "AEIOU" else "a ") + text


@register.filter
def luggage_phrase(reservation_data):
    """Spoken luggage, e.g. '3 checked bags' / '2 carry-on bags' / 'no luggage'."""
    rd = reservation_data or {}
    try:
        count = int(str(rd.get("luggage_count", 0)).strip() or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return "no luggage"
    kind = (rd.get("luggage_type") or "").strip()
    word = "bag" if count == 1 else "bags"
    if kind == "carry_on":
        return f"{count} carry-on {word}"
    if kind == "checked":
        return f"{count} checked {word}"
    return f"{count} {word}"


@register.filter
def carseat_phrase(reservation_data):
    """'1 rear-facing, 2 forward-facing and 1 booster'. Empty when there are none."""
    rd = reservation_data or {}

    def count(key):
        try:
            return int(str(rd.get(key, 0)).strip() or 0)
        except (TypeError, ValueError):
            return 0

    parts = []
    for key, word in (("rf_carseats", "rear-facing"),
                      ("ff_carseats", "forward-facing"),
                      ("booster_seats", "booster")):
        n = count(key)
        if n:
            parts.append(f"{n} {word}")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


@register.filter
def airline_spoken(name):
    """'DELTA' -> 'Delta' for the read-back; short codes like 'DL' stay as they are."""
    text = (name or "").strip()
    if len(text) > 3 and text.isupper():
        return text.title()
    return text
