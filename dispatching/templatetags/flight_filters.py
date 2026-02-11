from django import template
from django.utils import timezone
from datetime import datetime, timedelta

register = template.Library()


@register.filter
def flight_status_color(status):
    """
    Determine the color class for a flight status badge.
    
    Rules:
    - Red (bg-danger): delayed, cancelled, diverted, or any error/problem status
    - Green (bg-success): on time, landed, arrived, or any successful completion
    - Blue (bg-primary): scheduled, non-delayed, or normal status
    
    Args:
        status: Flight status string (e.g., "Delayed", "On Time", "Scheduled")
        
    Returns:
        Bootstrap color class string (e.g., "bg-danger", "bg-success", "bg-primary")
    """
    if not status:
        return "bg-secondary"
    
    status_lower = status.lower().strip()
    
    # Red: Problem statuses - delayed, cancelled, diverted, error states
    red_statuses = [
        'delayed', 'delay', 'cancelled', 'canceled', 'diverted', 
        'error', 'failed', 'problem', 'issues'
    ]
    if any(red_word in status_lower for red_word in red_statuses):
        return "bg-danger"
    
    # Green: On time, successful completion, landed, arrived, departed on time
    green_statuses = [
        'on time', 'ontime', 'landed', 'arrived', 'arrival',
        'departed', 'departure', 'completed', 'complete',
        'in flight', 'en route', 'in the air', 'took off'
    ]
    if any(green_word in status_lower for green_word in green_statuses):
        return "bg-success"
    
    # Blue: Scheduled, normal status, not yet departed
    blue_statuses = [
        'scheduled', 'schedule', 'boarding', 'gate', 'ready',
        'on schedule', 'normal', 'active'
    ]
    if any(blue_word in status_lower for blue_word in blue_statuses):
        return "bg-primary"
    
    # Default to blue for unknown statuses
    return "bg-primary"


@register.filter
def easy_time(value):
    """
    Format a datetime in an easy-to-read 12-hour format with AM/PM.

    Args:
        value: datetime object

    Returns:
        Formatted time string (e.g., "2:30 PM" or "11:45 AM")
    """
    if not value:
        return ""

    # Ensure we have a timezone-aware datetime
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())

    # Convert to local timezone
    local_time = timezone.localtime(value)

    # Format as 12-hour time with AM/PM (Windows-compatible)
    # Use %I for 12-hour format with leading zero, then strip it
    formatted = local_time.strftime("%I:%M %p")
    # Remove leading zero from hour (2:30 PM instead of 02:30 PM)
    if formatted[0] == '0':
        formatted = formatted[1:]
    return formatted  # e.g., "2:30 PM"


@register.filter
def easy_datetime(value):
    """
    Format a datetime in an easy-to-read format with date and 12-hour time.

    Args:
        value: datetime object

    Returns:
        Formatted datetime string (e.g., "Jan 15, 2:30 PM")
    """
    if not value:
        return ""

    # Ensure we have a timezone-aware datetime
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())

    # Convert to local timezone
    local_time = timezone.localtime(value)

    # Format as date + 12-hour time with AM/PM (Windows-compatible)
    formatted = local_time.strftime("%b %d, %I:%M %p")
    # Remove leading zero from hour
    parts = formatted.split(', ')
    if len(parts) == 2 and parts[1][0] == '0':
        parts[1] = parts[1][1:]
        formatted = ', '.join(parts)
    return formatted  # e.g., "Jan 15, 2:30 PM"


@register.filter
def dict_lookup(dictionary, key):
    """Look up a key in a dictionary. Usage: {{ mydict|dict_lookup:item.id }}"""
    if dictionary is None:
        return None
    return dictionary.get(key)


@register.filter
def time_ago(value):
    """
    Convert a datetime to a relative time string (e.g., "5 minutes ago", "2 hours ago").

    Args:
        value: datetime object

    Returns:
        Relative time string
    """
    if not value:
        return ""

    # Ensure we have a timezone-aware datetime
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())

    # Calculate time difference
    now = timezone.now()
    diff = now - value

    # Handle future times (shouldn't happen, but just in case)
    if diff.total_seconds() < 0:
        return "just now"

    # Convert to friendly format
    seconds = diff.total_seconds()

    if seconds < 60:
        return "just now"
    elif seconds < 3600:  # Less than 1 hour
        minutes = int(seconds / 60)
        return f"{minutes} min ago" if minutes > 1 else "1 min ago"
    elif seconds < 86400:  # Less than 1 day
        hours = int(seconds / 3600)
        return f"{hours} hr ago" if hours > 1 else "1 hr ago"
    elif seconds < 604800:  # Less than 1 week
        days = int(seconds / 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"
    else:
        # For longer periods, just show the date
        local_time = timezone.localtime(value)
        return local_time.strftime("%b %d")  # e.g., "Jan 15"


@register.filter
def fmt_dur(minutes):
    """
    Format a duration in minutes as a human-friendly string.
    Under 60m: "42m"
    60m+: "1h 21m"
    Exactly on the hour: "2h"

    Usage: {{ value|fmt_dur }}
    """
    if minutes is None:
        return ""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return ""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining == 0:
        return f"{hours}h"
    return f"{hours}h {remaining}m"

