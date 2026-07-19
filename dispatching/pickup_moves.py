"""
Shared write path for flight-driven pickup-time moves.

Both the dispatcher-facing match endpoints (dispatching.views) and the
guest-facing flight-verification auto-adjust (dispatching.flight_verify_views)
funnel through apply_pickup_time_move() so every flight-driven move gets the
same board "time changed" stamping + durable AuditLog row. Lives in its own
small module so flight_verify_views never imports dispatching.views
(circular-import risk).
"""

import logging

from django.utils import timezone

from reservations.models import AuditLog, Leg

logger = logging.getLogger(__name__)


def apply_pickup_time_move(leg, new_time, user=None, note="Flight match"):
    """
    Single write path for a flight-driven pickup-time move. Uses a queryset
    .update() like the original code (skips simple_history + the expensive
    save() work), so the board's "time changed" stamp fields are set here
    explicitly, preserving the earliest pre-change time across successive
    moves until a dispatcher acknowledges. A move that lands back on the
    pending "was" time (A→B→A before anyone acked) CLEARS the pending change
    instead of leaving a nonsense "was 10:00 → now 10:00" badge. Also writes
    a durable AuditLog row so the change survives even without an open ops
    task (user may be None for guest-triggered moves). No-op when the time
    is unchanged. Returns True if the pickup actually moved.
    """
    old_time = leg.pickup_time
    if old_time == new_time:
        return False

    now = timezone.now()
    if leg.has_unacked_time_change and new_time == leg.pickup_time_was:
        # Net-zero revert: the still-unacked change just moved back to its
        # original time — clear the badge instead of stamping it.
        pickup_time_changed_at = None
        pickup_time_was = None
    else:
        # Keep the earliest "was" while a change is still unacknowledged so
        # back-to-back moves don't hide the originally booked time from the badge.
        pickup_time_changed_at = now
        pickup_time_was = leg.pickup_time_was if leg.has_unacked_time_change else old_time
    Leg.objects.filter(id=leg.id).update(
        pickup_time=new_time,
        pickup_time_changed_at=pickup_time_changed_at,
        pickup_time_was=pickup_time_was,
        pickup_change_ack_at=None,
    )
    # Mirror onto the in-memory instance so callers see the applied state.
    leg.pickup_time = new_time
    leg.pickup_time_changed_at = pickup_time_changed_at
    leg.pickup_time_was = pickup_time_was
    leg.pickup_change_ack_at = None
    leg._original_pickup_time = new_time

    def _fmt(t):
        return t.strftime("%I:%M %p").lstrip("0") if t else ""

    try:
        AuditLog.objects.create(
            model_name="Leg",
            object_id=leg.id,
            action="updated",
            field_name="pickup_time",
            old_value=_fmt(old_time),
            new_value=_fmt(new_time),
            user=user,
            username=user.username if user else "guest",
            notes=note,
        )
    except Exception as e:
        logger.warning(f"Pickup-move audit log failed for leg {leg.id}: {e}")

    return True
