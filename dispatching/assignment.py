"""Single front door for driver-assignment writes (Schedule Sandbox core).

THE RULE: all code that changes WHO DRIVES a leg calls set_leg_driver().
The sandbox routing decision — stage in the draft overlay vs write live —
lives here and only here, so every current and FUTURE write path gets
held-day behavior for free instead of having to remember the gate.

Enforcement, not convention: a pre_save tripwire on Leg (connected in
DispatchingConfig.ready) raises SandboxLeakError in DEBUG/tests whenever
something changes Leg.driver directly while the leg's day is held. In
production it logs an error instead of breaking the request.

Two sanctioned ways to write live while a day is held:
- set_leg_driver(..., live_override=True): the dispatcher's "Edit live"
  emergency hatch — writes live AND mirrors into the overlay so a later
  publish won't revert the fix.
- sanctioned_live_write(): for fact-writes that must always be live
  (refund/cancellation unassigns) and for publish itself. Wrap the save,
  leave a comment saying why it's a fact.
"""
import logging
import sys
import threading
from contextlib import contextmanager

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_tl = threading.local()

# Strict mode raises on a leak instead of just logging. Default: tests and
# DEBUG runs (catch ungated write paths before they ship); prod logs only so
# an unusual-but-legitimate edit (e.g. Django admin) never breaks a request.
_STRICT_DEFAULT = bool(settings.DEBUG or ("test" in sys.argv))


class SandboxLeakError(Exception):
    """A live Leg.driver write happened on a held day outside the front door."""


@contextmanager
def sanctioned_live_write():
    """Mark Leg.driver writes inside this block as deliberate live writes."""
    prev = getattr(_tl, "sanctioned", False)
    _tl.sanctioned = True
    try:
        yield
    finally:
        _tl.sanctioned = prev


def is_sanctioned():
    return getattr(_tl, "sanctioned", False)


def can_use_sandbox(user):
    """Whether a user may build/hold sandbox (draft) schedules.

    Superusers (managers) always can. Other dispatchers need the
    'reservations.use_schedule_sandbox' permission, granted per-user (or via a
    group) in the Django admin. Users WITHOUT it edit the live schedule exactly
    as before — a held day never affects them.
    """
    return bool(
        getattr(user, "is_authenticated", False)
        and user.has_perm("reservations.use_schedule_sandbox")
    )


def _active_draft_for_date(target_date):
    """Return the active (non-terminal) ScheduleDraft for a date, or None.

    One cheap indexed lookup on (schedule_date, state). The partial unique
    constraint guarantees at most one active draft per date, so .first() is exact.
    """
    if target_date is None:
        return None
    from reservations.models import ScheduleDraft
    return ScheduleDraft.objects.filter(
        schedule_date=target_date,
        state__in=ScheduleDraft.ACTIVE_STATES,
    ).first()


def _log_draft_event(draft, event_type, actor=None, note="", **metadata):
    """Append a ScheduleDraftEvent to the draft timeline (audit + feedback)."""
    from reservations.models import ScheduleDraftEvent
    return ScheduleDraftEvent.objects.create(
        draft=draft,
        event_type=event_type,
        actor=actor,
        note=note or "",
        metadata=metadata or {},
    )


def _upsert_draft_assignment(draft, leg, driver, user, **event_meta):
    """Write the overlay delta for one leg WITHOUT touching Leg.driver.

    This is the side-effect firewall: it never calls leg.save(), so none of the
    Leg.save() side effects (pay calc, gratuity split, NTFY status signal, ops
    task-close) fire during drafting. They fire correctly at publish time.

    `driver` may be None to mean "draft says unassigned" (a row with
    proposed_driver=NULL), which is distinct from "no row" (defer to live).
    Extra kwargs are merged into the logged event's metadata (e.g. live_override).
    """
    from reservations.models import DraftAssignment, ScheduleDraftEvent
    da, _ = DraftAssignment.objects.update_or_create(
        draft=draft,
        leg=leg,
        defaults={
            "proposed_driver": driver,
            "assigned_by": user,
            "assigned_at": timezone.now(),
        },
    )
    meta = {"leg_id": leg.id, "to_driver": driver.id if driver else None}
    meta.update(event_meta)
    _log_draft_event(draft, ScheduleDraftEvent.EventType.EDITED, actor=user, **meta)
    return da


def set_leg_driver(leg, driver, actor, *, live_override=False, source=""):
    """THE front door: assign (or unassign, driver=None) a leg's driver.

    Held day + granted actor + no override  -> staged in the draft overlay
                                               (drivers see nothing).
    Otherwise                               -> live write, with audit fields
                                               and the ops-task attribution
                                               markers the legacy inline
                                               writes carried.
    live_override on a held day             -> live write, mirrored into the
                                               overlay so publish won't revert.

    Returns (mode, draft): mode is "staged" or "live"; draft is the active
    ScheduleDraft when one influenced the decision, else None.
    """
    draft = _active_draft_for_date(leg.pickup_date)
    sandbox_active = bool(draft) and actor is not None and can_use_sandbox(actor)

    if sandbox_active and not live_override:
        _upsert_draft_assignment(draft, leg, driver, actor,
                                 **({"source": source} if source else {}))
        return "staged", draft

    leg.driver = driver
    leg.driver_assigned_by = actor
    leg.driver_assigned_at = timezone.now()
    # Attribute conflict/tight-turn task auto-close (ops/signals.py) and the
    # auto-reset of leg.status on unassign (Leg.save) to the acting user.
    leg._reassigned_by = actor
    if driver is None:
        leg._status_change_user = actor
    with sanctioned_live_write():
        # Single save: Leg.save() auto-fills pay when driver changes.
        leg.save(update_fields=["driver", "driver_assigned_by", "driver_assigned_at"])

    if sandbox_active and live_override:
        # Emergency live edit on a held day by a granted user: mirror into the
        # overlay so a later publish won't revert this fix.
        _upsert_draft_assignment(draft, leg, driver, actor, live_override=True,
                                 **({"source": source} if source else {}))
    return "live", (draft if sandbox_active else None)


# ── Tripwire ────────────────────────────────────────────────────────────────

def _leg_driver_tripwire(sender, instance, **kwargs):
    """pre_save alarm: Leg.driver changing on a HELD day outside the front door.

    Cost when no draft is active for the date: one indexed EXISTS at most —
    and zero for saves whose update_fields don't touch driver, for unsaved
    legs (new bookings are facts), and for past dates.
    """
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and not ({"driver", "driver_id"} & set(update_fields)):
        return
    if is_sanctioned() or not instance.pk or instance.pickup_date is None:
        return
    try:
        if instance.pickup_date < timezone.localdate():
            return  # historical edits (statements etc.) can't leak a draft
    except TypeError:
        return
    from reservations.models import ScheduleDraft
    if not ScheduleDraft.objects.filter(
        schedule_date=instance.pickup_date,
        state__in=ScheduleDraft.ACTIVE_STATES,
    ).exists():
        return
    old_driver_id = (
        sender.objects.filter(pk=instance.pk)
        .values_list("driver_id", flat=True)
        .first()
    )
    if old_driver_id == instance.driver_id:
        return
    msg = (
        f"SANDBOX LEAK: Leg {instance.pk} driver changed live "
        f"({old_driver_id} -> {instance.driver_id}) while {instance.pickup_date} "
        f"is held by an active draft. Route this write through "
        f"dispatching.assignment.set_leg_driver() (or wrap a deliberate "
        f"fact-write in sanctioned_live_write())."
    )
    logger.error(msg)
    import os
    strict = os.environ.get("SANDBOX_TRIPWIRE_STRICT")
    if (strict or "").lower() in ("1", "true") or (strict is None and _STRICT_DEFAULT):
        raise SandboxLeakError(msg)


def install_tripwire():
    """Connect the tripwire. Called from DispatchingConfig.ready() in ALL
    process types (including tests — that's where it earns its keep)."""
    from django.db.models.signals import pre_save
    from reservations.models import Leg
    pre_save.connect(
        _leg_driver_tripwire, sender=Leg,
        dispatch_uid="sandbox_leg_driver_tripwire",
    )
