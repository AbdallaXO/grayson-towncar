"""
Canonical selector for office dispatch staff — the roster the staffing/coverage
board renders and the schedule editor writes to.

An "office dispatcher" is a subtractive heuristic (there is no is_dispatcher
field by design): an active is_staff user who is NOT flagged as a driver or a
travel agent on their UserProfile.

NOTE — deliberately NOT the same as the staff-metrics allowlist. The two metrics
views inline a *different* variant (``is_staff=True`` with no ``is_active``
filter, returning a set of ids) so deactivated staff are retained in historical
reports. Those are left alone; only the board uses this queryset.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


def office_staff_qs():
    """Active office dispatchers, ordered by first name.

    Reads ``UserProfile`` once for the driver/agent exclusion set. Superusers
    are intentionally NOT excluded: a founder who works dispatch shifts is a
    legitimate row, and (per the coverage math) dropping a scheduled superuser
    would falsely make a coworker read as working "alone."
    """
    from users.models import UserProfile

    non_office = set(
        UserProfile.objects.filter(Q(is_driver=True) | Q(is_travel_agent=True))
        .values_list("user_id", flat=True)
    )
    return (
        User.objects.filter(is_staff=True, is_active=True)
        .exclude(id__in=non_office)
        .order_by("first_name", "username")
    )
