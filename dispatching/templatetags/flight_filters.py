from django import template

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

