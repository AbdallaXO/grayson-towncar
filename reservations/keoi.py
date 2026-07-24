"""KEOI ("Keep Eye On It") lifecycle service — auto-close / auto-reactivate.

These two functions are the central enforcement point for the KEOI flag
lifecycle. They are called from the Leg pre_save/post_save signal pair
(``reservations/signals.py``) on every terminal status transition, so every
leg-completion pathway (board AJAX, driver portal, bulk, refund-cancellation)
is covered without touching individual call sites.

Both are idempotent and race-safe:
  * close uses a single queryset ``.update()`` on the active row;
  * reactivate no-ops if an active flag already exists and swallows the
    IntegrityError from a concurrent reactivation (partial-unique constraint).

Audit rows are written directly against ``AuditLog`` (model_name='Leg') so they
surface in the existing Leg History modal for free. We write directly rather
than through ``create_audit_log`` so unattributed system events fall back to
``username="guest"`` — the ``dispatching/pickup_moves.py:74`` convention — which
``create_audit_log`` cannot express (it only resolves username from a user).
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _resolve_actor(actor):
    """Return an authenticated User or None. Falls back to the thread-local
    request user (driver-portal completions attribute to the driver's User)."""
    if actor is None:
        try:
            from reservations.middleware import get_current_user
            actor = get_current_user()
        except Exception:
            actor = None
    if actor is not None and hasattr(actor, "is_authenticated") and not actor.is_authenticated:
        actor = None
    return actor


def _audit(leg, field_name, old_value, new_value, actor, notes):
    """Write one AuditLog row against the Leg. Unattributed events get
    username='guest' (mirrors dispatching/pickup_moves.py)."""
    try:
        from reservations.models import AuditLog
        AuditLog.objects.create(
            model_name="Leg",
            object_id=leg.id,
            action="updated",
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
            user=actor if actor else None,
            username=actor.username if actor else "guest",
            notes=notes,
        )
    except Exception as e:  # never let auditing break the status transition
        logger.warning(f"KEOI audit ({field_name}) failed for leg {leg.id}: {e}")


def close_active_keoi(leg, reason, actor=None):
    """Close the active KEOI flag on ``leg`` (if any) with ``reason``.

    ``reason`` is a LegKeoi.ClosedReason value (typically 'leg_completed' or
    'leg_cancelled'). Idempotent: returns 0 when there is nothing to close.
    Returns the number of rows closed (0 or 1).
    """
    from reservations.models import LegKeoi

    actor = _resolve_actor(actor)
    updated = (
        LegKeoi.objects.filter(leg=leg, closed_at__isnull=True)
        .update(
            closed_at=timezone.now(),
            closed_reason=reason,
            closed_by=actor if actor else None,
        )
    )
    if updated:
        _audit(
            leg,
            field_name="keoi_closed",
            old_value="active",
            new_value=reason,
            actor=actor,
            notes=f"KEOI auto-closed: {reason}",
        )
    return updated


def reactivate_keoi(leg, actor=None):
    """Reactivate the most-recently auto-closed KEOI flag on ``leg`` when the
    leg leaves a terminal status.

    No-op when an active flag already exists. Only rows closed with an
    auto-close reason ('leg_completed'/'leg_cancelled') are eligible —
    admin-removed flags NEVER reactivate. Concurrent reactivation loses
    gracefully via the partial-unique constraint. Returns 1 on reactivation,
    else 0.
    """
    from reservations.models import LegKeoi

    actor = _resolve_actor(actor)
    if LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).exists():
        return 0
    row = (
        LegKeoi.objects.filter(
            leg=leg,
            closed_reason__in=(
                LegKeoi.ClosedReason.LEG_COMPLETED,
                LegKeoi.ClosedReason.LEG_CANCELLED,
            ),
        )
        .order_by("-closed_at")
        .first()
    )
    if row is None:
        return 0
    prior_reason = row.closed_reason
    row.closed_at = None
    row.closed_reason = None
    row.closed_by = None
    row.updated_by = actor if actor else None
    try:
        with transaction.atomic():
            row.save(update_fields=["closed_at", "closed_reason", "closed_by",
                                    "updated_by", "updated_at"])
    except IntegrityError:
        # Another request reactivated (or created) an active flag first; the
        # partial-unique constraint rejected ours. Leave the winner in place.
        return 0
    _audit(
        leg,
        field_name="keoi_reactivated",
        old_value=prior_reason,
        new_value="active",
        actor=actor,
        notes=f"KEOI reactivated (leg left terminal status; was {prior_reason})",
    )
    return 1
