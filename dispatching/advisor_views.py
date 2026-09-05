"""Recovery Advisor endpoints — state feed, one-click apply, snooze.

Thin views only (separate module like ``keoi_views``); the real work lives elsewhere:

  * STATE (GET) is strictly READ-ONLY board analysis. It short-circuits on an unchanged
    board fingerprint (``compute_board_fingerprint`` — 3 indexed queries + sha1, the same
    budget as the driver-portal poll this copies), and on change serves the full card set
    from ``conflict_advisor.compute_advisor_state``, cached per (date, fingerprint) for
    ``RA_CARDS_TTL_S`` so multiple dispatcher tabs share ONE computation. While computing
    today's board it also mirrors the visible critical-card count into ``ra_crit_count``
    for the navbar badge (which is cache-READ-only and never computes).
  * APPLY (POST) is an exact shim in the ``views.farmout_apply`` shape over
    ``conflict_advisor_actions.apply_advisor_plan`` — parse -> lock -> staleness ->
    hard rules -> whole-board revalidation -> snapshot -> front-door writes all happen
    there, never here.
  * SNOOZE (POST) hides one card board-globally (owner decision: shared across
    tabs/dispatchers) via the ``ra_snoozed_{date}`` cache list — zero migrations,
    auto-expiring, capped at ``RA_SNOOZE_CAP_MIN``. Snoozing NEVER closes ops tasks.
  * FILE TASK (POST) turns a card's ``file_task`` offer (guard 9: only cards with
    NO open linked task carry one) into an OperationalTask through
    ``ops.services.create_task`` — inheriting its dedup + 2-hour cooldown, so
    re-clicking (or racing the 30-min scanner) never duplicates a task; a dedup
    hit answers with the EXISTING open task's id so the card can deep-link it.

Auth matches ``farmout_apply``: ``@login_required`` + staff check -> JSON 403;
``@require_POST`` on the writes. NEVER call drivers.utils.get_drive_time / AeroAPI /
Samsara HTTP / live Google from here — the advisor path is polled.
"""

import hashlib
import json
import logging
import time as _time
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

logger = logging.getLogger(__name__)

RA_CARDS_TTL_S = 120        # shared (date, fingerprint)-keyed compute cache
# Card-shape version, baked into every advisor cache key. The fingerprint tracks
# the BOARD, not the code — so without this a deploy that changes the card shape
# keeps serving the previous shape until the TTL drains. Bump it whenever the
# serialized card or plan dict gains, loses or redefines a field.
#   2 — dispatcher-facing `display` block (advisor_display.py)
#   3 — `file_task.disruption_id` (the ledger's join back to the card)
RA_CARD_SHAPE_V = 3
RA_CRIT_TTL_S = 300         # navbar badge mirror (today only)
RA_SNOOZE_DEFAULT_MIN = 30
RA_SNOOZE_CAP_MIN = 240     # owner decision: snooze is 30 min default, 4 h cap

# The only task types a card's file_task offer may create (the engine's kind →
# type mapping: unassigned → driver_assign, everything else → driver_conflict).
RA_FILE_TASK_TYPES = ("driver_conflict", "driver_assign")


# ── Rollout gate ────────────────────────────────────────────────────────────
# The advisor is SUPERUSER-ONLY while the owner trials it. Dispatchers are
# staff, so gating on is_staff would expose it to the whole floor. Every
# surface asks this one function — the four endpoints below, the dashboard
# rail, the conflict-task block and the navbar badge — so opening it up later
# is a ONE-LINE change here, not a hunt through templates.
#
#   To release to dispatchers: return ``user.is_staff``.
def advisor_visible_to(user):
    """True if this user may see the Recovery Advisor at all."""
    return bool(getattr(user, "is_authenticated", False)
                and getattr(user, "is_superuser", False))


def _parse_day(raw):
    """'YYYY-MM-DD' -> date, else None (callers pick their own fallback)."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _active_snoozes(day):
    """(cache_key, live entries) for a date — expired entries pruned on read.
    Entries are ``{"id": disruption_id, "until": epoch_seconds}``; the list is
    board-global (shared across tabs and dispatchers)."""
    key = f"ra_snoozed_{day.isoformat()}"
    now = _time.time()
    entries = [e for e in (cache.get(key) or [])
               if isinstance(e, dict) and e.get("until", 0) > now]
    return key, entries


def _rail_fingerprint(board_fp, snoozes, farm_pending):
    """The fingerprint the CLIENT round-trips: board hash + a digest of the
    rail's cache-only overlays (active snoozes, farmed-awaiting-confirm).
    Folding the overlays in makes the short-circuit honest on a quiet board:
    a snooze placed in another tab, an EXPIRED snooze, or an aged-out farm
    reminder all change the digest, so the next poll re-renders instead of
    hiding behind ``{"changed": false}`` until some leg field moves."""
    digest = hashlib.sha1(repr((
        sorted(e["id"] for e in snoozes),
        sorted((e["leg_id"], e["affiliate"]) for e in farm_pending),
    )).encode()).hexdigest()[:10]
    return f"{board_fp}.{digest}"


def _farm_pending_cards(farm_pending):
    """Synthetic watch cards for legs farmed via the advisor and not yet
    confirmed by the affiliate (SOP: board assignment is not acceptance).
    Rendered by the same client path as engine cards; never carry plans."""
    from dispatching.conflict_advisor import _FARM_CONFIRM_LINE

    cards = []
    for e in farm_pending:
        cards.append({
            "id": f"farm_pending:{e['leg_id']}",
            "kind": "farm_pending",
            "severity": "watch",
            "headline": f"Farmed — awaiting {e['affiliate']} confirm "
                        f"(leg {e['leg_id']})",
            "narrative": _FARM_CONFIRM_LINE.format(aff=e["affiliate"]),
            "impact_at": None,
            "leg_ids": [e["leg_id"]],
            "task_id": None,
            "basis": "",
            "plans": [],
            "detected_only": False,
            "no_internal_solution": False,
        })
    return cards


@login_required
@require_http_methods(["GET"])
def recovery_advisor_state(request):
    """Advisor rail feed: ``?date=&fp=[&leg=]``.

    With a matching ``fp`` the response is ``{"changed": false, "fingerprint"}`` and
    nothing is recomputed. Otherwise: full card state (cached per fingerprint),
    snoozed cards filtered out (and counted), the held-day flag, and — when serving
    today's unfiltered board — the ``ra_crit_count`` navbar mirror refreshed."""
    if not advisor_visible_to(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    from dispatching.conflict_advisor import (compute_advisor_state,
                                              compute_board_fingerprint)
    from dispatching.conflict_advisor_actions import list_farm_pending

    d = _parse_day(request.GET.get("date")) or timezone.localdate()
    board_fp = compute_board_fingerprint(d)
    _, snoozes = _active_snoozes(d)
    farm_pending = list_farm_pending(d)
    current_fp = _rail_fingerprint(board_fp, snoozes, farm_pending)
    if request.GET.get("fp") == current_fp:
        return JsonResponse({"changed": False, "fingerprint": current_fp})

    try:
        for_leg_id = int(request.GET.get("leg", ""))
    except (ValueError, TypeError):
        for_leg_id = None

    # Leg-filtered requests (task detail) bypass the shared cache: for_leg_id
    # narrows analysis BEFORE the per-card budget/cap, so it's not a subset of
    # the cached full state. The compute cache stays keyed on the BOARD hash —
    # snooze/farm-overlay churn re-filters, never re-computes.
    cache_key = f"ra_cards_v{RA_CARD_SHAPE_V}_{d.isoformat()}_{board_fp}"
    state = cache.get(cache_key) if for_leg_id is None else None
    if state is None:
        state = compute_advisor_state(d, for_leg_id=for_leg_id)
        if for_leg_id is None:
            cache.set(cache_key, state, RA_CARDS_TTL_S)

    snoozed_ids = {e["id"] for e in snoozes}
    cards = [c for c in state["disruptions"] if c["id"] not in snoozed_ids]
    n_snoozed = len(state["disruptions"]) - len(cards)
    # "Farmed — awaiting affiliate confirm" reminders ride after the engine
    # cards (watch band; snoozable like any card).
    cards += [c for c in _farm_pending_cards(farm_pending)
              if c["id"] not in snoozed_ids]

    if for_leg_id is None and d == timezone.localdate():
        crit = sum(1 for c in cards if c.get("severity") == "critical")
        cache.set("ra_crit_count", crit, RA_CRIT_TTL_S)

    # Ledger (Phase 1.2, invisible). Records the cards ACTUALLY SENT — after
    # the snooze filter, farm-pending reminders included — because that is the
    # set a screen received. Deliberately NOT on the short-circuit branch
    # (which is pinned to three queries) and NOT at compute time (the card
    # cache is shared across tabs for 120 s, so a compute is not a showing).
    # Upserts by episode in two writes whatever the card count, and swallows
    # its own failures: the rail must never go dark because a log row didn't.
    from dispatching import advisor_events
    advisor_events.record_cards(
        d, cards, source="task" if for_leg_id is not None else "rail")

    from dispatching.assignment import _active_draft_for_date, can_use_sandbox
    held = _active_draft_for_date(d) is not None
    return JsonResponse({
        "changed": True,
        "fingerprint": current_fp,
        "computed_at": state["computed_at"],
        "truncated": state["truncated"],
        "held": held,
        # Owner decision: staging is the SECONDARY choice, offered only to
        # sandbox-granted users while a draft holds the day.
        "can_stage": bool(held and can_use_sandbox(request.user)),
        "snoozed": n_snoozed,
        "disruptions": cards,
    })


@login_required
@require_POST
def recovery_advisor_apply(request):
    """Apply one advisor plan — the rail's write endpoint.

    Thin JSON shim over ``conflict_advisor_actions.apply_advisor_plan``, which
    re-validates CURRENT state (staleness 409, VIP/departure/pending-refund hard
    rules, whole-board feasibility) and writes only through ``set_leg_driver`` /
    ``apply_pickup_time_move`` (the front doors), so held-day policy and all
    assignment side effects behave like any dispatch-board edit."""
    if not advisor_visible_to(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from dispatching.conflict_advisor_actions import apply_advisor_plan
    status, payload = apply_advisor_plan(data, request.user)
    return JsonResponse(payload, status=status)


@login_required
@require_POST
def recovery_advisor_file_task(request):
    """File an ops task from a card's ``file_task`` offer:
    ``{date, leg_id, task_type, title}``.

    Creation goes through ``ops.services.create_task`` (never a raw
    ``OperationalTask.objects.create``) so the advisor inherits the same dedup
    and closed-task cooldown every scanner obeys. When dedup swallows the
    create, the response carries the already-open task's id (``created:
    false``) — the card links to it instead of growing a twin."""
    if not advisor_visible_to(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    d = _parse_day(data.get("date"))
    if d is None:
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)
    try:
        leg_id = int(data.get("leg_id"))
    except (ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid leg_id"}, status=400)
    task_type = str(data.get("task_type") or "").strip()
    if task_type not in RA_FILE_TASK_TYPES:
        return JsonResponse({"success": False, "error": "Invalid task_type"}, status=400)

    from reservations.models import Leg
    leg = (Leg.objects.filter(id=leg_id, pickup_date=d)
           .select_related("reservation").first())
    if leg is None:
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    title = str(data.get("title") or "").strip()[:200] or f"Conflict on leg {leg_id}"
    from ops.models import OperationalTask
    from ops.services import create_task
    task = create_task(
        task_type=task_type,
        title=title,
        priority=OperationalTask.Priority.HIGH,
        description="Filed from the Recovery Advisor.",
        reservation=leg.reservation,
        leg=leg,
        created_by=request.user,
        metadata={"source": "conflict_advisor",
                  "driver_id": leg.driver_id},
    )
    from dispatching import advisor_events
    card_id = str(data.get("disruption_id") or "").strip()

    if task is not None:
        advisor_events.record_task_filed(d, card_id, task_id=task.id,
                                         created=True, user=request.user)
        return JsonResponse({"success": True, "created": True, "task_id": task.id})

    # Dedup/cooldown hit — hand back the open twin (if any) for the deep-link.
    existing = (OperationalTask.objects.filter(
        leg_id=leg_id, task_type=task_type,
        status__in=list(OperationalTask.OPEN_STATUSES))
        .order_by("id").values_list("id", flat=True).first())
    # created=False is the honest signal, not a failure: the 30-minute scanner
    # had already filed the same task, which is what "superseded" means here.
    advisor_events.record_task_filed(d, card_id, task_id=existing,
                                     created=False, user=request.user)
    return JsonResponse({"success": True, "created": False, "task_id": existing})


@login_required
@require_POST
def recovery_advisor_snooze(request):
    """Snooze one card: ``{date, disruption_id, minutes=30}`` (capped 240).

    Board-global by design — the cache list is shared, so a card one dispatcher
    snoozes disappears for everyone until it expires. Dismissal never closes the
    linked ops task (the card offers full task snooze separately)."""
    if not advisor_visible_to(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    d = _parse_day(data.get("date"))
    if d is None:
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)
    disruption_id = str(data.get("disruption_id") or "").strip()
    if not disruption_id:
        return JsonResponse(
            {"success": False, "error": "disruption_id is required"}, status=400)
    raw_minutes = data.get("minutes", RA_SNOOZE_DEFAULT_MIN)
    try:
        minutes = int(raw_minutes)
    except (ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid minutes"}, status=400)
    minutes = max(1, min(minutes, RA_SNOOZE_CAP_MIN))

    key, entries = _active_snoozes(d)
    now = _time.time()
    entries = ([e for e in entries if e["id"] != disruption_id]
               + [{"id": disruption_id, "until": now + minutes * 60}])
    # TTL rides the longest-lived entry (each already capped), so the key
    # self-expires with its last snooze.
    ttl = max(1, int(max(e["until"] for e in entries) - now))
    cache.set(key, entries, ttl)
    # The snooze itself is cache-only and gone within four hours, so this row
    # is the only lasting record that a card was dismissed, and by whom.
    from dispatching import advisor_events
    advisor_events.record_snoozed(d, disruption_id, minutes=minutes,
                                  user=request.user)
    return JsonResponse({
        "success": True,
        "disruption_id": disruption_id,
        "snoozed_minutes": minutes,
        "active_snoozes": len(entries),
    })
