"""
Resolution-ladder playbooks for Ops Control task detail pages.

A *playbook* is the business config for one ``OperationalTask.TaskType``:
an ordered list of resolution **steps** (call → text → email, or
match-flight → cover-in-house → farm-out, …) plus a headline metric and a
human description of what "resolved" means.

The companion :func:`build_ladder_steps` turns a playbook + a task's real
``CommunicationAttempt`` history into render-ready step dicts — de-emphasising
channels already tried, highlighting the next-best step, and attaching a
*soft* nudge (never a hard block) when a dispatcher works ahead of the
recommended step. This mirrors ``OperationalTask.blocked_by``'s documented
soft-dependency behaviour.

This is code config, not DB: the steps ARE business logic. The detail view
picks a playbook by ``task.task_type`` via :func:`get_playbook` (which falls
back to a generic single-action layout for unknown / unconfigured types), and
renders every type through the one shared ladder component.

Keyed by the raw ``TaskType`` *values* (e.g. ``"payment_chase"``) so this
module has no import-time dependency on the ops models.
"""

# ── Icon vocabulary (Bootstrap Icons, matching the rest of the app) ──
# Channel / action → bi-* class. Single source of truth for ladder icons.
ICON = {
    "call": "bi-telephone-fill",
    "voicemail": "bi-voicemail",
    "sms": "bi-chat-dots",
    "email": "bi-envelope",
    "payment": "bi-credit-card",
    "cancel": "bi-x-circle",
    "trip": "bi-geo-alt",
    "flight": "bi-airplane",
    "clock": "bi-clock-history",
    "assign": "bi-person-check",
    "broadcast": "bi-megaphone",
    "farm_out": "bi-box-arrow-up-right",
    "verify": "bi-patch-check",
    "note": "bi-pencil-square",
}


# ── Action registry ──
# Branch / step actions reference these by id. ``kind`` tells the page JS what
# to do; only payment_chase actions are fully wired this pass — coverage /
# verify actions are declared so their playbooks render, and get wired when
# those pages are built.
ACTIONS = {
    # payment_chase — resolve branch (call answered)
    "take_payment": {
        "label": "Take payment", "icon": ICON["payment"],
        "kind": "payment", "style": "gold",
        "hint": "Opens the payment portal, then resolves the task once paid.",
    },
    "cancel_trip": {
        "label": "Cancel trip", "icon": ICON["cancel"],
        "kind": "cancel", "style": "ghost",
        "hint": "Opens the reservation to cancel, then resolves the task.",
    },
    # payment_chase — advance branch (call not answered)
    "leave_voicemail": {
        "label": "Leave voicemail", "icon": ICON["voicemail"],
        "kind": "log", "style": "ghost",
        "log_channel": "call", "log_outcome": "voicemail",
        "hint": "Logs a voicemail attempt and moves to the next step.",
    },
    # coverage cascades (declared; wired when those pages are built)
    "cover_in_house": {
        "label": "Cover in-house", "icon": ICON["assign"],
        "kind": "assign", "style": "gold",
    },
    "broadcast": {
        "label": "Broadcast to drivers", "icon": ICON["broadcast"],
        "kind": "broadcast", "style": "ghost",
    },
    "farm_out": {
        "label": "Farm out", "icon": ICON["farm_out"],
        "kind": "farm_out", "style": "ghost",
    },
    "match_flight": {
        "label": "Match flight time", "icon": ICON["clock"],
        "kind": "match_flight", "style": "ghost",
    },
}


# ── Playbooks, keyed by TaskType value ──
# headline: {source, label, format} — which context key holds the metric, its
#   unit label, and how to render it (money | minutes | count | since | text).
# steps[*]: id, order, channel(call|sms|email|None), label, icon, recommended,
#   description, optional template (named draft) and branches (call step only).
# resolves_when: human description of the resolution condition.
PLAYBOOKS = {
    # ════════ Shape A — contact cascade (reach the person; resolve on contact) ════════
    "payment_chase": {
        "headline": {"source": "pc_amount_owed", "label": "owed", "format": "money"},
        "steps": [
            {
                "id": "call", "order": 1, "channel": "call",
                "label": "Call", "icon": ICON["call"], "recommended": True,
                "description": (
                    "Reach the guest directly — the fastest resolution. If they "
                    "answer, take payment or cancel the trip on the spot."
                ),
                "branches": {
                    "answered": ["take_payment", "cancel_trip"],
                    "no_answer": ["leave_voicemail"],
                },
            },
            {
                "id": "text", "order": 2, "channel": "sms",
                "label": "Text", "icon": ICON["sms"], "recommended": False,
                "template": "payment_sms",
                "description": (
                    "If no answer, send a short text with the payment link. "
                    "Draft it here, send from your device, and log it."
                ),
            },
            {
                "id": "email", "order": 3, "channel": "email",
                "label": "Email", "icon": ICON["email"], "recommended": False,
                "template": "payment_reminder",
                "description": (
                    "Lowest leverage — a payment-reminder email with the "
                    "checkout link. Use after a call and a text."
                ),
            },
        ],
        "resolves_when": "paid_or_cancelled",
    },
    "contact_form": {
        "headline": {"source": "cf_hours_since", "label": "since inquiry", "format": "since"},
        "steps": [
            {"id": "call", "order": 1, "channel": "call", "label": "Call",
             "icon": ICON["call"], "recommended": True,
             "description": "Call the prospect while the inquiry is hot — quote and book on the call.",
             "branches": {"answered": ["quote_trip", "mark_dead"], "no_answer": ["leave_voicemail"]}},
            {"id": "text", "order": 2, "channel": "sms", "label": "Text",
             "icon": ICON["sms"], "recommended": False, "template": "contact_sms",
             "description": "No answer — text a quick reply and offer to quote."},
            {"id": "email", "order": 3, "channel": "email", "label": "Email quote",
             "icon": ICON["email"], "recommended": False, "template": "lead_quote",
             "description": "Send a written quote by email as the fallback."},
        ],
        "resolves_when": "booked_or_dead",
    },
    "confirmation_texts": {
        "headline": {"source": "ct_unsent", "label": "to confirm", "format": "count"},
        "steps": [
            {"id": "text", "order": 1, "channel": "sms", "label": "Text",
             "icon": ICON["sms"], "recommended": True, "template": "confirmation_sms",
             "description": "Send the confirmation text — the primary channel; goal is a guest confirmation."},
            {"id": "call", "order": 2, "channel": "call", "label": "Call",
             "icon": ICON["call"], "recommended": False,
             "description": "No reply to the text — call to confirm pickup details.",
             "branches": {"answered": ["mark_confirmed"], "no_answer": ["leave_voicemail"]}},
            {"id": "email", "order": 3, "channel": "email", "label": "Email",
             "icon": ICON["email"], "recommended": False, "template": "confirmation_email",
             "description": "Email the confirmation as a last resort."},
        ],
        "resolves_when": "guest_confirms",
    },

    # ════════ Shape B — coverage cascade (in-house first, farm out last) ════════
    "driver_conflict": {
        "headline": {"source": "conflict_minutes", "label": "min behind gate", "format": "minutes"},
        "steps": [
            {"id": "match_flight", "order": 1, "channel": None, "label": "Match flight time",
             "icon": ICON["clock"], "recommended": False,
             "description": "Move the booked pickup to the flight's gate arrival — corrects the target window."},
            {"id": "cover_in_house", "order": 2, "channel": None, "label": "Cover in-house",
             "icon": ICON["assign"], "recommended": True,
             "description": "Reassign the leg to a free in-house driver — the primary fix, no margin lost.",
             "branches": {"resolve": ["cover_in_house"]}},
            {"id": "farm_out", "order": 3, "channel": None, "label": "Farm out",
             "icon": ICON["farm_out"], "recommended": False,
             "description": "Hand the leg to a partner operator. Costs margin — only after in-house is exhausted.",
             "branches": {"resolve": ["farm_out"]}},
        ],
        "resolves_when": "leg_covered",
    },
    "driver_assign": {
        "headline": {"source": "da_pickup_time_str", "label": "time to trip", "format": "text"},
        "steps": [
            {"id": "auto_assign", "order": 1, "channel": None, "label": "Auto-assign best in-house",
             "icon": ICON["assign"], "recommended": True,
             "description": "Assign the strongest available in-house driver for the window.",
             "branches": {"resolve": ["cover_in_house"]}},
            {"id": "broadcast", "order": 2, "channel": None, "label": "Broadcast to available drivers",
             "icon": ICON["broadcast"], "recommended": False,
             "description": "Offer the leg to all available drivers if no clear best fit."},
            {"id": "farm_out", "order": 3, "channel": None, "label": "Farm out",
             "icon": ICON["farm_out"], "recommended": False,
             "description": "Hand to a partner operator as the last resort.",
             "branches": {"resolve": ["farm_out"]}},
        ],
        "resolves_when": "leg_assigned",
    },
    "tight_turn": {
        "headline": {"source": "tt_late_minutes", "label": "min behind", "format": "minutes"},
        "steps": [
            {"id": "recheck", "order": 1, "channel": None, "label": "Re-check live timing",
             "icon": ICON["clock"], "recommended": True,
             "description": "Pull live flight + drive data — the turn may already be safe."},
            {"id": "buffer", "order": 2, "channel": None, "label": "Add buffer / adjust pickup",
             "icon": ICON["clock"], "recommended": False,
             "description": "Nudge the pickup or build slack so the turn is comfortable."},
            {"id": "reassign", "order": 3, "channel": None, "label": "Reassign in-house",
             "icon": ICON["assign"], "recommended": False,
             "description": "Move the leg to a driver who can make the turn.",
             "branches": {"resolve": ["cover_in_house"]}},
            {"id": "farm_out", "order": 4, "channel": None, "label": "Farm out",
             "icon": ICON["farm_out"], "recommended": False,
             "description": "Farm out only if no in-house driver can make it safely.",
             "branches": {"resolve": ["farm_out"]}},
        ],
        "resolves_when": "turn_made_safe",
    },

    # ════════ Shape C — verify cascade ════════
    "flight_verify": {
        "headline": {"source": "fv_flight_arrival_str", "label": "matched arrival", "format": "text"},
        "steps": [
            {"id": "auto_match", "order": 1, "channel": None, "label": "Auto-match flight API",
             "icon": ICON["verify"], "recommended": True,
             "description": "Pull live flight data — if it agrees with the reservation, this resolves with no human action."},
            {"id": "confirm_guest", "order": 2, "channel": "sms", "label": "Confirm flight # with guest",
             "icon": ICON["sms"], "recommended": False, "template": "flight_verify_sms",
             "description": "Data disagrees — text or call the guest to confirm the flight number."},
            {"id": "correct", "order": 3, "channel": None, "label": "Correct # & adjust pickup",
             "icon": ICON["clock"], "recommended": False,
             "description": "Fix the flight number and re-match the pickup to the real arrival."},
        ],
        "resolves_when": "flight_verified",
    },
    "afterhours_fee": {
        "headline": {"source": "ah_amount", "label": "fee owed", "format": "money"},
        "steps": [
            {"id": "charge", "order": 1, "channel": None, "label": "Add fee + charge card on file",
             "icon": ICON["payment"], "recommended": True,
             "description": "Apply the after-hours fee and charge the saved card.",
             "branches": {"resolve": ["take_payment"]}},
            {"id": "call", "order": 2, "channel": "call", "label": "Call",
             "icon": ICON["call"], "recommended": False,
             "description": "No card on file — call the guest about the fee.",
             "branches": {"answered": ["take_payment", "waive_fee"], "no_answer": ["leave_voicemail"]}},
            {"id": "text", "order": 3, "channel": "sms", "label": "Text",
             "icon": ICON["sms"], "recommended": False, "template": "afterhours_sms",
             "description": "Text the guest the fee + payment link."},
            {"id": "email", "order": 4, "channel": "email", "label": "Email",
             "icon": ICON["email"], "recommended": False, "template": "afterhours_email",
             "description": "Email the fee notice as the fallback."},
        ],
        "resolves_when": "fee_paid_or_waived",
    },

    # ════════ Manual — no fixed ladder ════════
    "manual": {
        "headline": None,
        "steps": [],
        "resolves_when": "resolved_by_staff",
    },
}


# Generic fallback for unknown / unconfigured task types — a single
# "log an action then resolve" panel (no fixed ladder).
GENERIC_PLAYBOOK = {
    "headline": None,
    "steps": [],
    "resolves_when": "resolved_by_staff",
}


def get_playbook(task_type):
    """Return the playbook for a ``TaskType`` value, or the generic fallback."""
    return PLAYBOOKS.get(task_type, GENERIC_PLAYBOOK)


# Outcomes that mean a comm step actually *connected* (resolution possible).
_RESOLVING_OUTCOMES = frozenset({"answered"})


def _tried_pill(channel, count):
    """'sent ×2' for text/email, 'tried ×2' for calls, '' when untried."""
    if count <= 0:
        return ""
    verb = "sent" if channel in ("sms", "email") else "tried"
    return f"{verb} ×{count}"


def build_ladder_steps(playbook, comm_attempts):
    """
    Merge a playbook with a task's real ``CommunicationAttempt`` history into
    render-ready step dicts for the shared ladder component.

    State rules (all *soft* — never disable a later step):
    - Count prior attempts per channel; a step that's been tried renders
      de-emphasised with a "sent ×N" / "tried ×N" pill.
    - The recommended (gold) step is the **first step with no successful
      resolution** — in practice the first untried step. Logging a
      non-resolving outcome (voicemail / no-answer) therefore moves the
      highlight to the next step on the next render. If every step has been
      tried, the highlight falls back to the playbook's recommended step.
    - Steps that sit *ahead* of the recommended step carry a gentle nudge
      pointing back to it (the soft-nudge — "X not logged yet …").

    Args:
        playbook: a PLAYBOOKS entry (or GENERIC_PLAYBOOK).
        comm_attempts: iterable of objects/dicts with ``.channel`` (and
            optionally ``.outcome``) — e.g. ``task.comm_attempts.all()``.

    Returns:
        list[dict] ordered by step ``order``; ``[]`` for ladder-less playbooks.
    """
    raw_steps = sorted(playbook.get("steps") or [], key=lambda s: s.get("order", 0))
    if not raw_steps:
        return []

    # Tally attempts per channel.
    per_channel = {}
    resolved_channels = set()
    for a in comm_attempts:
        ch = getattr(a, "channel", None) if not isinstance(a, dict) else a.get("channel")
        if not ch:
            continue
        per_channel[ch] = per_channel.get(ch, 0) + 1
        outcome = getattr(a, "outcome", None) if not isinstance(a, dict) else a.get("outcome")
        if outcome in _RESOLVING_OUTCOMES:
            resolved_channels.add(ch)

    def tried_count(step):
        ch = step.get("channel")
        return per_channel.get(ch, 0) if ch else 0

    # Recommended (gold) step:
    #  • Fresh task (no attempt history) → honor the playbook's flagged
    #    recommended step. This is what coverage/verify cascades want (their
    #    steps are non-comm, so there's never comm history to advance through).
    #  • Once there's history → advance to the first step with no successful
    #    resolution (first untried comm step, a connected channel counts as
    #    satisfied). If everything's been tried, fall back to the flagged step.
    config_rec_idx = next(
        (i for i, s in enumerate(raw_steps) if s.get("recommended")), 0
    )
    if not per_channel:
        recommended_idx = config_rec_idx
    else:
        recommended_idx = None
        for i, s in enumerate(raw_steps):
            ch = s.get("channel")
            connected = ch in resolved_channels if ch else False
            if tried_count(s) == 0 and not connected:
                recommended_idx = i
                break
        if recommended_idx is None:
            recommended_idx = config_rec_idx

    rec_step = raw_steps[recommended_idx]
    rec_is_comm = bool(rec_step.get("channel"))

    out = []
    for i, s in enumerate(raw_steps):
        count = tried_count(s)
        if i == recommended_idx:
            state = "recommended"
        elif count > 0 and s.get("channel"):
            # Only comm steps fade once tried; non-comm steps never deemphasize
            # (they can't be "tried" in the communication sense).
            state = "deemphasized"
        else:
            state = "available"

        # Soft nudge on every step past the recommended one (never a hard block).
        nudge = ""
        if i > recommended_idx:
            rec_label = rec_step["label"].lower()
            if rec_is_comm:
                nudge = f"No {rec_label} logged yet — {rec_label} resolves this fastest."
            else:
                nudge = f"Recommended: do “{rec_label}” first."

        out.append({
            "id": s["id"],
            "order": s.get("order", i + 1),
            "label": s["label"],
            "icon": s.get("icon", ICON["note"]),
            "channel": s.get("channel"),
            "description": s.get("description", ""),
            "template": s.get("template"),
            "recommended": i == recommended_idx,
            "state": state,
            "tried_count": count,
            "tried_pill": _tried_pill(s.get("channel"), count),
            "nudge": nudge,
            "branches": s.get("branches") or {},
        })
    return out


def resolve_actions(action_ids):
    """Map a list of action ids (from a step's branches) to their configs.

    Unknown ids are skipped. Each returned dict includes its ``id`` so the
    template can wire ``data-action``.
    """
    resolved = []
    for aid in action_ids or []:
        cfg = ACTIONS.get(aid)
        if cfg:
            resolved.append({"id": aid, **cfg})
    return resolved
