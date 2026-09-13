"""
State machine for the Open / Close shift checklists.

These are the ONLY functions that mutate a checklist, its rows or its
exceptions — the same discipline `ops/services.py` keeps for the time clock,
and for the same reason: a state machine with one door stays consistent.

WHAT THIS MODULE NEVER DOES (docs/dispatch-ops/SHIFT-SYSTEM-AUDIT.md §13.3):
write a Leg, a leg status, a task, a watch flag, a vehicle plan, a draft or a
scheduler dial. The shift layer reads the dispatch system and records what the
office did about it. Nothing here can change what is on the board.
"""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import shift_checks
from .models import (
    HUMAN_ROW_LABELS,
    SYSTEM_ROW_LABELS,
    ShiftChecklist,
    ShiftChecklistRow,
    ShiftException,
    ShiftSettings,
    StaffActivity,
)

logger = logging.getLogger(__name__)


class ShiftError(Exception):
    """A checklist transition that is not allowed (e.g. completing with a silent gap)."""


def _log(user, action, **metadata):
    """Best-effort activity row. Never allowed to break a checklist action."""
    try:
        StaffActivity.objects.create(
            user=user, action_type=action, metadata=metadata,
        )
    except Exception:
        logger.warning("Could not log shift activity %s for %s", action, user)


# ── Who is down to open and close ─────────────────────────────────────

def scheduled_duties(target_date):
    """``(opener, closer)`` for ``target_date`` — either may be None.

    Deliberately ASKS THE STAFFING BOARD rather than re-deriving it. The board's
    rule (ops/coverage.py::_resolve_duty) is that an explicit assignment wins,
    and with none set the earliest person in opens / the latest out closes.
    Re-implementing that here would give the floor two answers to one question
    the first time the two drifted — the exact failure the audit warns about
    (§13.4).

    Both duties come back from ONE roster load. They are always wanted together
    (the navbar asks on every page), and resolving a day costs a prefetched
    roster query plus a coverage pass — worth doing once, not twice.

    Read-only: resolving a schedule writes nothing.
    """
    from . import coverage
    from .staff import office_staff_qs

    roster = list(
        office_staff_qs().prefetch_related(
            "weekly_schedule_rows", "schedule_overrides", "extra_shifts",
        )
    )
    if not roster:
        return None, None
    try:
        day = coverage.dated_range([target_date], roster)["weekdays"][0]
    except Exception:
        logger.exception("Could not resolve the duties for %s", target_date)
        return None, None

    def _user(duty):
        if not duty:
            return None
        return next((u for u in roster if u.id == duty["uid"]), None)

    return _user(day.get("opener")), _user(day.get("closer"))


def scheduled_opener(target_date):
    """The dispatcher the roster says opens ``target_date``, or None."""
    return scheduled_duties(target_date)[0]


def scheduled_closer(target_date):
    """The dispatcher the roster says closes ``target_date``, or None."""
    return scheduled_duties(target_date)[1]


def opening_prompt_for(user, target_date=None):
    """Where to send someone who has just clocked in, or None to leave them be.

    Only the person down to open, only while today's opening is unfinished. A
    dispatcher clocking in mid-afternoon, or arriving after the opening is done,
    is not interrupted.
    """
    from django.urls import reverse

    target_date = target_date or timezone.localdate()
    opener = scheduled_opener(target_date)
    if opener is None or opener.pk != user.pk:
        return None
    checklist = ShiftChecklist.objects.filter(
        date=target_date, kind=ShiftChecklist.Kind.OPEN,
    ).first()
    if checklist is not None and checklist.is_complete:
        return None
    return f"{reverse('shift_open')}?date={target_date:%Y-%m-%d}"


# ── Opening ───────────────────────────────────────────────────────────

def get_or_open_checklist(date, kind, user=None, now=None):
    """The checklist for ``(date, kind)``, created with its rows on first open.

    Idempotent: two dispatchers opening the page at once get the same row, and
    the unique constraint settles any race.
    """
    now = now or timezone.now()
    checklist = ShiftChecklist.objects.filter(date=date, kind=kind).first()
    if checklist:
        return checklist

    cfg = ShiftSettings.load()
    system_keys, human_keys = cfg.rows_for(kind)

    try:
        with transaction.atomic():
            checklist = ShiftChecklist.objects.create(
                date=date, kind=kind, opened_by=user, opened_at=now,
                targets=cfg.targets_for(kind),
            )
            position = 0
            for key in system_keys:
                ShiftChecklistRow.objects.create(
                    checklist=checklist, key=key,
                    kind=ShiftChecklistRow.Kind.SYSTEM, position=position,
                )
                position += 1
            for key in human_keys:
                ShiftChecklistRow.objects.create(
                    checklist=checklist, key=key,
                    kind=ShiftChecklistRow.Kind.HUMAN, position=position,
                )
                position += 1
            _carry_forward(checklist, now=now)
    except IntegrityError:
        # Lost the race; the other request created it.
        return ShiftChecklist.objects.get(date=date, kind=kind)

    if user is not None:
        _log(user, StaffActivity.ActionType.SHIFT_OPENED,
             checklist_id=checklist.id, kind=kind, date=str(date))
    return checklist


def previous_checklist(checklist):
    """The shift whose leftovers carry into this one.

    Close of the previous day feeds the next Open; the same day's Open feeds its
    Close. That is the actual handover chain the floor works.
    """
    if checklist.kind == ShiftChecklist.Kind.OPEN:
        return ShiftChecklist.objects.filter(
            date=checklist.date - timedelta(days=1),
            kind=ShiftChecklist.Kind.CLOSE,
        ).first()
    return ShiftChecklist.objects.filter(
        date=checklist.date, kind=ShiftChecklist.Kind.OPEN,
    ).first()


def _carry_forward(checklist, now=None):
    """Copy the previous shift's unresolved exceptions onto this one.

    Each carried note keeps a link to its source (`carried_from`), so a note on
    its third shift can say so, and must be acknowledged before Board Safe —
    which is what stops a leftover quietly ageing for a week.
    """
    source = previous_checklist(checklist)
    if source is None:
        return 0

    carried = 0
    for old in source.exceptions.filter(resolved_at__isnull=True).select_related("owner"):
        ShiftException.objects.create(
            checklist=checklist,
            what=old.what,
            owner=old.owner,
            next_action=old.next_action,
            next_action_at=old.next_action_at,
            task=old.task, leg=old.leg, keoi=old.keoi,
            created_by=old.created_by,
            carried_from=old,
        )
        carried += 1
    return carried


# ── The live system rows ──────────────────────────────────────────────

def reconcile_rows(checklist):
    """Bring an unfinished checklist into line with the current settings.

    Without this, a change to which rows exist only ever reaches TOMORROW's
    checklist, and today's keeps showing a row that has been retired — which is
    how a raw "missed_calls" key ended up in front of the floor.

    Deliberately conservative: rows that somebody has already confirmed or
    written a note against are HISTORY and are left alone. Only an untouched row
    that no longer belongs is removed.
    """
    if checklist.is_complete:
        return
    cfg = ShiftSettings.load()
    system_keys, human_keys = cfg.rows_for(checklist.kind)
    wanted = list(system_keys) + list(human_keys)

    existing = {r.key: r for r in checklist.rows.all()}

    for position, key in enumerate(wanted):
        row = existing.get(key)
        kind = (ShiftChecklistRow.Kind.SYSTEM if key in system_keys
                else ShiftChecklistRow.Kind.HUMAN)
        if row is None:
            ShiftChecklistRow.objects.create(
                checklist=checklist, key=key, kind=kind, position=position,
            )
        elif row.position != position or row.kind != kind:
            row.position = position
            row.kind = kind
            row.save(update_fields=["position", "kind", "updated_at"])

    for key, row in existing.items():
        if key in wanted:
            continue
        if row.state == ShiftChecklistRow.State.OPEN and not row.exceptions.exists():
            row.delete()


def refresh_system_rows(checklist):
    """Recompute every system row and return ``{key: CheckResult}``.

    Called on every render. The row's stored state follows the live count: zero
    means clear, non-zero means outstanding unless an exception already covers
    it. A system row is never confirmed by hand — that is enforced here by
    simply never reading `confirmed_by` for it.
    """
    reconcile_rows(checklist)
    system_rows = [r for r in checklist.rows.all() if r.is_system]
    results = shift_checks.run_checks(
        [r.key for r in system_rows], checklist.target_date,
    )

    documented = {
        e.row_id for e in checklist.exceptions.filter(resolved_at__isnull=True)
        if e.row_id
    }

    for row in system_rows:
        result = results.get(row.key)
        if result is None:
            continue
        if result.count == 0:
            state = ShiftChecklistRow.State.CLEAR
        elif row.id in documented:
            state = ShiftChecklistRow.State.DOCUMENTED
        else:
            state = ShiftChecklistRow.State.OPEN
        if row.state != state or row.last_count != result.count:
            row.state = state
            row.last_count = result.count
            row.save(update_fields=["state", "last_count", "updated_at"])

    return results


def _sync_human_row_states(checklist):
    """A human row covered by an open exception reads as documented, not outstanding."""
    documented = {
        e.row_id for e in checklist.exceptions.filter(resolved_at__isnull=True)
        if e.row_id
    }
    for row in checklist.rows.all():
        if row.is_system or row.state == ShiftChecklistRow.State.CLEAR:
            continue
        state = (ShiftChecklistRow.State.DOCUMENTED if row.id in documented
                 else ShiftChecklistRow.State.OPEN)
        if row.state != state:
            row.state = state
            row.save(update_fields=["state", "updated_at"])


# ── Human rows ────────────────────────────────────────────────────────

def confirm_human_row(checklist, key, user, now=None):
    """Tap-to-confirm one of the queues the app cannot see."""
    now = now or timezone.now()
    if checklist.is_complete:
        raise ShiftError("This checklist is already finished. Reopen it first.")

    row = checklist.rows.filter(key=key).first()
    if row is None:
        raise ShiftError("That check isn't on this list.")
    if row.is_system:
        raise ShiftError(
            "That one counts itself — it clears when the work is done, not by ticking."
        )

    row.state = ShiftChecklistRow.State.CLEAR
    row.confirmed_by = user
    row.confirmed_at = now
    row.save(update_fields=["state", "confirmed_by", "confirmed_at", "updated_at"])
    _log(user, StaffActivity.ActionType.SHIFT_ROW_CONFIRMED,
         checklist_id=checklist.id, row=key)
    return row


def unconfirm_human_row(checklist, key, user, now=None):
    """Undo a tap — a mis-click should not need an admin."""
    if checklist.is_complete:
        raise ShiftError("This checklist is already finished. Reopen it first.")
    row = checklist.rows.filter(key=key).first()
    if row is None or row.is_system:
        raise ShiftError("That check can't be changed.")
    row.state = ShiftChecklistRow.State.OPEN
    row.confirmed_by = None
    row.confirmed_at = None
    row.save(update_fields=["state", "confirmed_by", "confirmed_at", "updated_at"])
    _sync_human_row_states(checklist)
    return row


# ── Exceptions ────────────────────────────────────────────────────────

def document_exception(checklist, *, what, owner, next_action, user,
                       row_key=None, next_action_at=None,
                       task=None, leg=None, keoi=None):
    """Record something outstanding, with a name against it.

    All three parts are required. That is the rule the whole system turns on:
    a checklist cannot be finished by silence.
    """
    if checklist.is_complete:
        raise ShiftError("This checklist is already finished. Reopen it first.")
    what = (what or "").strip()
    next_action = (next_action or "").strip()
    if not what:
        raise ShiftError("Say what is outstanding.")
    if owner is None:
        raise ShiftError("Someone has to own it — pick a name.")
    if not next_action:
        raise ShiftError("Say what happens next.")

    row = checklist.rows.filter(key=row_key).first() if row_key else None

    exception = ShiftException.objects.create(
        checklist=checklist, row=row,
        what=what, owner=owner, next_action=next_action[:200],
        next_action_at=next_action_at,
        task=task, leg=leg, keoi=keoi,
        created_by=user,
    )
    if row is not None:
        row.state = ShiftChecklistRow.State.DOCUMENTED
        row.save(update_fields=["state", "updated_at"])
    _log(user, StaffActivity.ActionType.SHIFT_EXCEPTION_RAISED,
         checklist_id=checklist.id, exception_id=exception.id, row=row_key or "")
    return exception


def acknowledge_exception(exception, user, now=None):
    """Take ownership of a note carried over from the previous shift."""
    exception.acknowledged_by = user
    exception.acknowledged_at = now or timezone.now()
    exception.save(update_fields=["acknowledged_by", "acknowledged_at"])
    return exception


def resolve_exception(exception, user, note="", now=None):
    """Close a note out. It then stops carrying forward."""
    exception.resolved_at = now or timezone.now()
    exception.resolved_by = user
    exception.resolution_note = (note or "")[:200]
    exception.save(update_fields=["resolved_at", "resolved_by", "resolution_note"])
    _sync_human_row_states(exception.checklist)
    return exception


def reassign_exception(exception, owner, user):
    """Change who owns a note. Lead-gated at the view; unrestricted here."""
    exception.owner = owner
    exception.save(update_fields=["owner"])
    return exception


# ── Gates ─────────────────────────────────────────────────────────────

def gate_state(checklist):
    """What still stands between this checklist and each gate.

    Returns ``{board_safe_ready, complete_ready, blocking_system,
    blocking_human, unacknowledged, exceptions_missing_owner}``.
    """
    rows = list(checklist.rows.all())
    open_exceptions = list(
        checklist.exceptions.filter(resolved_at__isnull=True).select_related("owner")
    )

    blocking_system = [
        r for r in rows
        if r.is_system and r.state == ShiftChecklistRow.State.OPEN
    ]
    blocking_human = [
        r for r in rows
        if not r.is_system and r.state == ShiftChecklistRow.State.OPEN
    ]
    unacknowledged = [e for e in open_exceptions if e.needs_acknowledgement]

    return {
        "blocking_system": blocking_system,
        "blocking_human": blocking_human,
        "unacknowledged": unacknowledged,
        "board_safe_ready": not blocking_system and not unacknowledged,
        "complete_ready": (
            not blocking_system and not blocking_human and not unacknowledged
        ),
        "open_exceptions": open_exceptions,
    }


def mark_board_safe(checklist, user, now=None):
    """Stamp Board Safe once the system rows are clear or documented.

    Recomputes the counts first. The page refreshes them on render, but this is
    also reachable straight from the action endpoint, and judging a gate on a
    stale row would refuse a dispatcher whose board is genuinely clean.
    """
    now = now or timezone.now()
    if checklist.board_safe_at:
        return checklist
    refresh_system_rows(checklist)
    _sync_human_row_states(checklist)
    state = gate_state(checklist)
    if not state["board_safe_ready"]:
        raise ShiftError(
            "Board safe needs every counted row at zero, or handed to someone, "
            "and anything carried over from last shift taken by a name."
        )
    checklist.board_safe_at = now
    checklist.save(update_fields=["board_safe_at", "updated_at"])
    return checklist


def complete_checklist(checklist, user, now=None):
    """Finish the checklist. Refuses while anything is outstanding and unexplained."""
    now = now or timezone.now()
    if checklist.is_complete:
        raise ShiftError("This one is already finished.")

    refresh_system_rows(checklist)
    _sync_human_row_states(checklist)
    state = gate_state(checklist)
    if not state["complete_ready"]:
        raise ShiftError(
            "Something is still outstanding and nobody has it. Use \u201cCan\u2019t "
            "clear it\u201d on that row to say who picks it up and what they do next."
        )

    if checklist.board_safe_at is None and checklist.kind == ShiftChecklist.Kind.OPEN:
        checklist.board_safe_at = now

    checklist.counts_snapshot = {
        r.key: r.last_count for r in checklist.rows.all() if r.is_system
    }
    checklist.completed_by = user
    checklist.completed_at = now
    checklist.save(update_fields=[
        "board_safe_at", "counts_snapshot", "completed_by", "completed_at", "updated_at",
    ])
    _log(user, StaffActivity.ActionType.SHIFT_COMPLETED,
         checklist_id=checklist.id, kind=checklist.kind, date=str(checklist.date))
    return checklist


def reopen_checklist(checklist, user, now=None):
    """Lead/admin action: put a finished checklist back into play."""
    if not checklist.is_complete:
        raise ShiftError("That one isn't finished.")
    checklist.completed_at = None
    checklist.completed_by = None
    checklist.reopened_by = user
    checklist.reopened_at = now or timezone.now()
    checklist.reopen_count = (checklist.reopen_count or 0) + 1
    checklist.save(update_fields=[
        "completed_at", "completed_by", "reopened_by", "reopened_at",
        "reopen_count", "updated_at",
    ])
    _log(user, StaffActivity.ActionType.SHIFT_REOPENED, checklist_id=checklist.id)
    return checklist


# ── The pasteable summary ─────────────────────────────────────────────

def _clock(dt):
    return timezone.localtime(dt).strftime("%-I:%M %p") if dt else ""


def build_summary(checklist):
    """The message a dispatcher pastes into the team's WhatsApp group.

    Written to the house rules in docs/release-notes/README.md: name the person
    and the consequence, no field names, no jargon, plain sentences. Founder
    direction 2026-09-12 — a person sends this, never a bot.
    """
    cfg = ShiftSettings.load()
    lines = []
    if cfg.summary_intro:
        lines.append(cfg.summary_intro)

    who = ""
    if checklist.completed_by:
        who = checklist.completed_by.get_full_name() or checklist.completed_by.username
    day = checklist.date.strftime("%A")
    word = "open" if checklist.kind == ShiftChecklist.Kind.OPEN else "close"

    headline = f"{day} {word} complete"
    if who:
        headline += f" — {who}"
    if checklist.completed_at:
        headline += f", {_clock(checklist.completed_at)}"

    if checklist.complete_status == "late":
        headline += " (past target)"
    elif checklist.complete_status == "on_time":
        headline += " (on time)"
    lines.append(headline)

    # Board safe earns its own line only when it was a distinct moment. On a
    # clean open both stamps land together, and repeating the same clock time
    # reads like a glitch.
    if checklist.kind == ShiftChecklist.Kind.OPEN and checklist.board_safe_at:
        same_moment = (
            checklist.completed_at is not None
            and abs(
                (checklist.completed_at - checklist.board_safe_at).total_seconds()
            ) < 60
        )
        if not same_moment:
            safe = f"Board safe {_clock(checklist.board_safe_at)}"
            if checklist.board_safe_status == "late":
                safe += " (past target)"
            lines.append(safe + ".")

    open_exceptions = list(
        checklist.exceptions.filter(resolved_at__isnull=True).select_related("owner")
    )
    if not open_exceptions:
        lines.append("Nothing outstanding.")
    else:
        for exception in open_exceptions:
            owner = (exception.owner.get_full_name() or exception.owner.username)
            line = f"{exception.what.strip().rstrip('.')}. {owner}: {exception.next_action.strip().rstrip('.')}"
            if exception.next_action_at:
                line += f" by {_clock(exception.next_action_at)}"
            lines.append(line + ".")

    return "\n".join(lines)


# ── The Lead's 7-day view ─────────────────────────────────────────────

def lead_rows(start_date, end_date):
    """One entry per day in the range, pairing that day's open and close."""
    from django.db.models import Prefetch

    checklists = (
        ShiftChecklist.objects
        .filter(date__range=(start_date, end_date))
        .select_related("opened_by", "completed_by", "reopened_by")
        .prefetch_related(
            Prefetch(
                "exceptions",
                queryset=ShiftException.objects.select_related(
                    "owner", "task", "leg", "carried_from"
                ),
            ),
            "rows",
        )
    )

    by_date = {}
    for checklist in checklists:
        by_date.setdefault(checklist.date, {})[checklist.kind] = checklist

    out, day = [], end_date
    while day >= start_date:
        entry = by_date.get(day, {})
        opened = entry.get(ShiftChecklist.Kind.OPEN)
        closed = entry.get(ShiftChecklist.Kind.CLOSE)
        outstanding = []
        for checklist in (opened, closed):
            if checklist:
                outstanding += [
                    e for e in checklist.exceptions.all() if e.resolved_at is None
                ]
        out.append({
            "date": day,
            "open": opened,
            "close": closed,
            "outstanding": outstanding,
            "carried": [e for e in outstanding if e.carried_from_id],
            "missing_open": opened is None,
            "missing_close": closed is None,
        })
        day -= timedelta(days=1)
    return out
