"""
Views for the Dispatch Shift System — Open Shift, Close Shift, and the Lead's
7-day view.

Access follows the house pattern (ops/views.py:70,74): `_is_staff` for the
floor's own pages, and a permission check for the Lead's. Every POST returns
JSON and turns a `ShiftError` into a soft message rather than a 500, so a
double-click just re-syncs the page — the same contract `timeclock_action`
already gives the floor.
"""

import json
import logging
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import shift_services
from .models import (
    HUMAN_ROW_HINTS,
    HUMAN_ROW_LABELS,
    SYSTEM_ROW_LABELS,
    ShiftChecklist,
    ShiftChecklistRow,
    ShiftException,
    ShiftSettings,
)
from .staff import office_staff_qs
from .views import _is_staff, _is_superuser

User = get_user_model()
logger = logging.getLogger(__name__)


def _can_review(user):
    """The Lead's view: the Dispatch Lead group, or an admin."""
    return user.is_superuser or user.has_perm("ops.review_checklists")


def _can_reopen(user):
    return user.is_superuser or user.has_perm("ops.reopen_checklist")


def _can_reassign_owner(user):
    return user.is_superuser or user.has_perm("ops.edit_exception_owner")


def _parse_date(raw, default):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return default


def _row_view(row, result=None):
    """One rendered checklist row."""
    return {
        "key": row.key,
        "label": row.label,
        "hint": HUMAN_ROW_HINTS.get(row.key, ""),
        "kind": row.kind,
        "state": row.state,
        "is_system": row.is_system,
        "count": result.count if result else None,
        "unknown": bool(result and result.count < 0),
        "items": result.items if result else [],
        "more": result.more if result else 0,
        "url": result.url if result else "",
        "url_label": result.url_label if result else "Board",
        "aside": result.aside if result else "",
        "aside_action": result.aside_action if result else "",
        "aside_count": result.aside_count if result else 0,
        "aside_items": result.aside_items if result else [],
        "tone": result.tone if result else "red",
        "confirmed_by": (
            row.confirmed_by.get_full_name() or row.confirmed_by.username
        ) if row.confirmed_by else "",
        "confirmed_at": row.confirmed_at,
    }


def _gate_tiles(checklist):
    """The two countdown tiles in the header.

    Each is one of three states, and the wording changes with it rather than
    saying "not yet" forever: counting down to a target, the time it was hit, or
    how far past it went.
    """
    from datetime import datetime

    tiles = []
    now = timezone.now()

    def tile(key, label, stamped_at):
        target = checklist._target_dt(key)
        # Which of the two limits is setting this gate — the SOP allowance
        # measured from the open, or the wall-clock backstop underneath it.
        basis = checklist.target_basis(key)
        minutes_allowed = (checklist.targets or {}).get(f"{key}_min")
        basis_note = (
            f"{minutes_allowed} min in" if basis == "allowance" and minutes_allowed
            else ""
        )
        if stamped_at is not None:
            late = bool(target and stamped_at > target)
            return {
                "label": label, "state": "late" if late else "ok", "pct": 100,
                "target": timezone.localtime(target).strftime("%-I:%M") if target else "",
                "value": timezone.localtime(stamped_at).strftime("%-I:%M %p"),
                "note": "past target" if late else "on time",
                "basis_note": basis_note,
            }
        if target is None:
            return {"label": label, "state": "", "target": "",
                    "value": "no target", "note": "", "pct": 0, "basis_note": ""}
        minutes = int((target - now).total_seconds() // 60)
        start = checklist.opened_at or checklist.created_at
        span = (target - start).total_seconds() if start else 0
        pct = 0
        if span > 0:
            pct = max(0, min(100, int((now - start).total_seconds() / span * 100)))
        if minutes >= 0:
            hrs, mins = divmod(minutes, 60)
            left = f"{hrs}h {mins}m" if hrs else f"{mins} min"
            return {"label": label, "state": "running",
                    "target": timezone.localtime(target).strftime("%-I:%M"),
                    "value": left, "note": "left", "pct": pct,
                    "basis_note": basis_note}
        over = abs(minutes)
        hrs, mins = divmod(over, 60)
        late = f"{hrs}h {mins}m" if hrs else f"{mins} min"
        return {"label": label, "state": "late",
                "target": timezone.localtime(target).strftime("%-I:%M"),
                "value": late, "note": "over", "pct": 100,
                "basis_note": basis_note}

    if checklist.kind == ShiftChecklist.Kind.OPEN:
        tiles.append(tile("board_safe", "Board safe by", checklist.board_safe_at))
        tiles.append(tile("open_complete", "Open complete by", checklist.completed_at))
    else:
        tiles.append(tile("close", "Close by", checklist.completed_at))
    return tiles


def _checklist_context(request, checklist):
    results = shift_services.refresh_system_rows(checklist)
    shift_services._sync_human_row_states(checklist)
    state = shift_services.gate_state(checklist)

    rows = list(checklist.rows.select_related("confirmed_by").all())
    system_rows = [_row_view(r, results.get(r.key)) for r in rows if r.is_system]
    human_rows = [_row_view(r) for r in rows if not r.is_system]

    exceptions = list(
        checklist.exceptions.filter(resolved_at__isnull=True)
        .select_related("owner", "created_by", "carried_from", "row")
    )
    carried = [e for e in exceptions if e.carried_from_id]
    raised_here = [e for e in exceptions if not e.carried_from_id]

    opener = None
    if checklist.kind == ShiftChecklist.Kind.OPEN:
        try:
            opener = shift_services.scheduled_opener(checklist.date)
        except Exception:
            logger.warning("Could not resolve the opener for %s", checklist.date)

    rows_all = system_rows + human_rows
    done = sum(1 for r in rows_all if r["state"] == ShiftChecklistRow.State.CLEAR)
    noted = sum(1 for r in rows_all if r["state"] == ShiftChecklistRow.State.DOCUMENTED)
    needs = sum(1 for r in rows_all if r["state"] == ShiftChecklistRow.State.OPEN)

    return {
        "checklist": checklist,
        "gates": _gate_tiles(checklist),
        "tally": {"done": done + noted, "total": len(rows_all), "needs": needs,
                  "noted": noted},
        "opener": opener,
        "viewer_is_opener": bool(opener and opener.pk == request.user.pk),
        "viewer_name": request.user.get_full_name() or request.user.username,
        "is_open_shift": checklist.kind == ShiftChecklist.Kind.OPEN,
        "target_date": checklist.target_date,
        "system_rows": system_rows,
        "human_rows": human_rows,
        "carried": carried,
        "exceptions": raised_here,
        "gate": state,
        "summary": shift_services.build_summary(checklist) if checklist.is_complete else "",
        "staff": list(office_staff_qs()),
        "row_choices": (
            [(r["key"], r["label"]) for r in system_rows + human_rows
             if r["state"] != ShiftChecklistRow.State.CLEAR]
        ),
        "can_reopen": _can_reopen(request.user),
        "can_reassign_owner": _can_reassign_owner(request.user),
        "can_review": _can_review(request.user),
        "today": timezone.localdate(),
        "now_local": timezone.localtime(timezone.now()),
        "previous": shift_services.previous_checklist(checklist),
    }


# ── Open / Close pages ────────────────────────────────────────────────

@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def shift_open(request):
    """Open Shift — today's checks, the human rows, and anything carried over."""
    date = _parse_date(request.GET.get("date"), timezone.localdate())
    checklist = shift_services.get_or_open_checklist(
        date, ShiftChecklist.Kind.OPEN, user=request.user,
    )
    context = _checklist_context(request, checklist)
    context["other_url"] = f"/dispatching/shift/close/?date={date:%Y-%m-%d}"
    return render(request, "dispatching/shift_checklist.html", context)


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def shift_close(request):
    """Close Shift — the same checks pointed at tomorrow, plus tomorrow's conflicts."""
    date = _parse_date(request.GET.get("date"), timezone.localdate())
    checklist = shift_services.get_or_open_checklist(
        date, ShiftChecklist.Kind.CLOSE, user=request.user,
    )
    context = _checklist_context(request, checklist)
    context["other_url"] = f"/dispatching/shift/open/?date={date:%Y-%m-%d}"
    return render(request, "dispatching/shift_checklist.html", context)


# ── Actions ───────────────────────────────────────────────────────────

def _payload(request):
    try:
        return json.loads(request.body or "{}")
    except (json.JSONDecodeError, AttributeError):
        return request.POST.dict()


def _load_checklist(data):
    checklist = ShiftChecklist.objects.filter(pk=data.get("checklist_id")).first()
    if checklist is None:
        raise shift_services.ShiftError("That checklist has gone — refresh the page.")
    return checklist


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
@require_POST
def shift_action(request):
    """One POST endpoint for every checklist action, mirroring `timeclock_action`."""
    data = _payload(request)
    action = data.get("action")

    try:
        checklist = _load_checklist(data)

        if action == "confirm_row":
            shift_services.confirm_human_row(checklist, data.get("key"), request.user)

        elif action == "unconfirm_row":
            shift_services.unconfirm_human_row(checklist, data.get("key"), request.user)

        elif action == "document":
            owner = User.objects.filter(pk=data.get("owner_id")).first()
            shift_services.document_exception(
                checklist,
                what=data.get("what", ""),
                owner=owner,
                next_action=data.get("next_action", ""),
                user=request.user,
                row_key=data.get("key") or None,
            )

        elif action == "acknowledge":
            exception = ShiftException.objects.filter(
                pk=data.get("exception_id"), checklist=checklist,
            ).first()
            if exception is None:
                raise shift_services.ShiftError("That note has gone — refresh the page.")
            shift_services.acknowledge_exception(exception, request.user)

        elif action == "resolve":
            exception = ShiftException.objects.filter(
                pk=data.get("exception_id"), checklist=checklist,
            ).first()
            if exception is None:
                raise shift_services.ShiftError("That note has gone — refresh the page.")
            shift_services.resolve_exception(
                exception, request.user, note=data.get("note", ""),
            )

        elif action == "reassign":
            if not _can_reassign_owner(request.user):
                raise shift_services.ShiftError(
                    "Only the dispatch lead can change who owns a note."
                )
            exception = ShiftException.objects.filter(
                pk=data.get("exception_id"), checklist=checklist,
            ).first()
            owner = User.objects.filter(pk=data.get("owner_id")).first()
            if exception is None or owner is None:
                raise shift_services.ShiftError("Pick a name and try again.")
            shift_services.reassign_exception(exception, owner, request.user)

        elif action == "acknowledge_pickup_changes":
            # Ticks the board's own purple "time changed" badges, through the
            # board's own endpoint semantics — the shift layer records that the
            # office looked, it does not invent a new kind of write.
            from reservations.models import Leg
            from . import shift_checks

            one = data.get("leg_id")
            if one:
                # Only ever a leg that is genuinely outstanding for this date —
                # an id from elsewhere must not be acknowledgeable from here.
                allowed = set(
                    shift_checks.unacked_pickup_change_ids(checklist.target_date)
                )
                leg_ids = [int(one)] if int(one) in allowed else []
            else:
                leg_ids = shift_checks.unacked_pickup_change_ids(checklist.target_date)
            if leg_ids:
                Leg.objects.filter(id__in=leg_ids).update(
                    pickup_change_ack_at=timezone.now(),
                )
            return JsonResponse({"success": True, "acked": len(leg_ids)})

        elif action == "board_safe":
            shift_services.mark_board_safe(checklist, request.user)

        elif action == "complete":
            shift_services.complete_checklist(checklist, request.user)

        elif action == "reopen":
            if not _can_reopen(request.user):
                raise shift_services.ShiftError(
                    "Only the dispatch lead can reopen a finished checklist."
                )
            shift_services.reopen_checklist(checklist, request.user)

        else:
            return JsonResponse({"success": False, "error": "Unknown action"}, status=400)

    except shift_services.ShiftError as exc:
        return JsonResponse({"success": False, "error": str(exc)})
    except Exception:
        logger.exception("Shift action %s failed", action)
        return JsonResponse({
            "success": False,
            "error": "That didn't go through. Refresh the page and try again.",
        })

    return JsonResponse({
        "success": True,
        "summary": (
            shift_services.build_summary(checklist) if checklist.is_complete else ""
        ),
    })


# ── The Lead's view ───────────────────────────────────────────────────

@login_required(login_url="login")
@user_passes_test(_can_review, login_url="dashboard")
def shift_lead(request):
    """The last N days of opens and closes, one row a day.

    Range controls follow `staff_kpis_view` (ops/views.py:2944): `?range=N`
    rolling, clamped, defaulting to a week.
    """
    try:
        days = int(request.GET.get("range", "7"))
    except (TypeError, ValueError):
        days = 7
    days = min(max(days, 1), 90)

    today = timezone.localdate()
    end_date = _parse_date(request.GET.get("to"), today)
    start_date = end_date - timedelta(days=days - 1)

    rows = shift_services.lead_rows(start_date, end_date)

    totals = {
        "opens_done": sum(1 for r in rows if r["open"] and r["open"].is_complete),
        "closes_done": sum(1 for r in rows if r["close"] and r["close"].is_complete),
        "days": len(rows),
        "outstanding": sum(len(r["outstanding"]) for r in rows),
        "carried": sum(len(r["carried"]) for r in rows),
        "board_safe_late": sum(
            1 for r in rows
            if r["open"] and r["open"].board_safe_status == "late"
        ),
        "complete_late": sum(
            1 for r in rows
            if r["open"] and r["open"].complete_status == "late"
        ),
    }

    return render(request, "dispatching/shift_lead.html", {
        "rows": rows,
        "totals": totals,
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
        "today": today,
        "can_reopen": _can_reopen(request.user),
    })
