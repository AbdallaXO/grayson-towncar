"""
Send window timing helpers for follow-up automation.

All follow-up messages are only sent between 8:00 AM and 9:00 PM US/Eastern.
If a task comes due outside this window, it is rescheduled to 8:15 AM the next morning.
"""

from datetime import time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

SEND_WINDOW_START = time(8, 0)   # 8:00 AM Eastern
SEND_WINDOW_END = time(21, 0)    # 9:00 PM Eastern
TIMEZONE = ZoneInfo("America/New_York")
MORNING_OFFSET_MINUTES = 15       # Queue for 8:15 AM to avoid burst at exactly 8:00


def to_eastern(dt=None):
    """Convert a datetime to US/Eastern. Defaults to now."""
    if dt is None:
        dt = timezone.now()
    return dt.astimezone(TIMEZONE)


def is_within_send_window(dt=None):
    """
    Check if the given datetime falls within the send window (8:00 AM - 9:00 PM Eastern).
    This check must happen at the moment of sending, not just at scheduling time.
    """
    eastern_dt = to_eastern(dt)
    return SEND_WINDOW_START <= eastern_dt.time() < SEND_WINDOW_END


def next_morning_slot(dt=None):
    """
    Return 8:15 AM Eastern on the next valid morning after the given datetime.
    If dt is before 8:00 AM, returns 8:15 AM the same day.
    If dt is at or after 8:00 AM, returns 8:15 AM the next day.
    """
    eastern_dt = to_eastern(dt)

    if eastern_dt.time() < SEND_WINDOW_START:
        # Before window today — use this morning
        morning = eastern_dt.replace(
            hour=SEND_WINDOW_START.hour,
            minute=MORNING_OFFSET_MINUTES,
            second=0, microsecond=0
        )
    else:
        # At or after window start — use tomorrow morning
        next_day = eastern_dt + timedelta(days=1)
        morning = next_day.replace(
            hour=SEND_WINDOW_START.hour,
            minute=MORNING_OFFSET_MINUTES,
            second=0, microsecond=0
        )

    return morning


def adjust_to_send_window(dt):
    """
    Adjust a datetime to fall within the send window.

    - If already within window, return as-is.
    - If before window (e.g. 3 AM), push to 8:15 AM same day.
    - If after window (e.g. 10 PM), push to 8:15 AM next day.

    Returns a timezone-aware datetime.
    """
    eastern_dt = to_eastern(dt)
    current_time = eastern_dt.time()

    if SEND_WINDOW_START <= current_time < SEND_WINDOW_END:
        # Already within window
        return dt

    if current_time < SEND_WINDOW_START:
        # Before window — same day morning
        adjusted = eastern_dt.replace(
            hour=SEND_WINDOW_START.hour,
            minute=MORNING_OFFSET_MINUTES,
            second=0, microsecond=0
        )
    else:
        # After window — next day morning
        next_day = eastern_dt + timedelta(days=1)
        adjusted = next_day.replace(
            hour=SEND_WINDOW_START.hour,
            minute=MORNING_OFFSET_MINUTES,
            second=0, microsecond=0
        )

    return adjusted
