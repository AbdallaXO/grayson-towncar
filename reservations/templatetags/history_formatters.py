"""
Template filters for formatting django-simple-history change values in admin.

Used so that TimeField and DateTimeField display in 12-hour AM/PM format
with proper timezone (local time) instead of 24-hour or raw UTC in the
history "Changes" column. DateField values display as "Mon, Mar 2".
"""
from datetime import date, datetime, time

from django import template
from django.utils import timezone
from django.utils.dateformat import DateFormat
from django.utils.dateparse import parse_datetime

register = template.Library()


def _time_to_12h(t):
    """Format a time object as 12-hour AM/PM (e.g. 2:48 PM)."""
    if t.hour == 0:
        hour12 = 12
        am_pm = "AM"
    elif t.hour < 12:
        hour12 = t.hour
        am_pm = "AM"
    elif t.hour == 12:
        hour12 = 12
        am_pm = "PM"
    else:
        hour12 = t.hour - 12
        am_pm = "PM"
    return f"{hour12}:{t.minute:02d} {am_pm}"


def _format_date_value(d):
    """Format a date object as e.g. 'Mon, Mar 2' (day without leading zero)."""
    return DateFormat(d).format("D, M j")


def _format_datetime_value(dt):
    """Format a datetime (aware or naive) in local time, 12-hour."""
    if dt is None:
        return ""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    local = timezone.localtime(dt)
    return DateFormat(local).format("n/j/Y g:i A")


@register.filter
def format_history_value(value):
    """
    Format a history delta value for display. Converts time and datetime
    values to 12-hour AM/PM in local timezone; formats date objects as
    'Mon, Mar 2'; passes other values through unchanged.
    """
    if value is None or value == "":
        return ""
    # datetime must be checked before date because datetime is a subclass of date
    if isinstance(value, datetime):
        return _format_datetime_value(value)
    if isinstance(value, date):
        return _format_date_value(value)
    if isinstance(value, time):
        return _time_to_12h(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return ""
        # Try parsing as full datetime first (Driver assigned at, Status changed at, etc.)
        dt = parse_datetime(value)
        if dt is None and " " in value:
            try:
                dt = datetime.fromisoformat(value.replace(" ", "T"))
            except (ValueError, TypeError):
                pass
        if dt is not None:
            return _format_datetime_value(dt)
        # Try parsing as ISO date string (e.g. "2026-03-02")
        try:
            d = date.fromisoformat(value)
            return _format_date_value(d)
        except (ValueError, TypeError):
            pass
        # Try parsing as time-only string from model_to_dict
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(value, fmt).time()
                return _time_to_12h(parsed)
            except (ValueError, TypeError):
                continue
    return value
