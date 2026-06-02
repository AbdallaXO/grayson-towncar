"""
Leads board — a date-anchored view of leads for a chosen pickup date.

Lets staff pick a date (e.g. a slow June 3) and see, at a glance:
  • who already BOOKED,
  • who we're ACTIVELY talking to (hands off — don't disturb),
  • who is a SAFE opportunity to make an offer to (fill the day),
  • who was already nudged, and who is lost / opted-out.

Manual actions (send an offer now / create a human follow-up / mark lost) reuse
the pre-pickup nudge engine's HARD safety guards, so an offer can never text an
opted-out number or double-send.
"""

import json
import logging
from datetime import datetime, time as dt_time, timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from reservations.models import Lead

logger = logging.getLogger(__name__)


def _is_staff(user):
    return user.is_staff or user.is_superuser


# "Are we actively talking to this lead right now?" tuning.
ACTIVE_REPLY_DAYS = 7        # replied within N days = still in conversation
ACTIVE_OUTBOUND_HOURS = 48   # we texted/emailed within N hours = recent touch

# Display order + labels for the board sections.
BUCKET_ORDER = ["safe", "active", "nudged", "booked", "lost", "opted_out"]
BUCKET_LABELS = {
    "safe": "Safe to offer",
    "active": "Active — hands off",
    "nudged": "Already nudged",
    "booked": "Booked",
    "lost": "Lost",
    "opted_out": "Opted out",
}


def _classify(lead, now, nudged_ids):
    """One of: booked / lost / opted_out / active / nudged / safe (first match)."""
    if lead.converted or lead.status == Lead.StatusChoices.CONVERTED:
        return "booked"
    if lead.status == Lead.StatusChoices.LOST:
        return "lost"
    if lead.sms_opt_out:
        return "opted_out"
    recent_reply = lead.last_reply_at and (now - lead.last_reply_at) <= timedelta(days=ACTIVE_REPLY_DAYS)
    recent_out = lead.last_contact_date and (now - lead.last_contact_date) <= timedelta(hours=ACTIVE_OUTBOUND_HOURS)
    if lead.needs_human_follow_up or recent_reply or recent_out or lead.sequence_active:
        return "active"
    if lead.id in nudged_ids:
        return "nudged"
    return "safe"


def _active_reason(lead, now):
    """The most informative reason a lead is 'active' (hands off)."""
    if lead.last_reply_at and (now - lead.last_reply_at) <= timedelta(days=ACTIVE_REPLY_DAYS):
        return "replied"
    if lead.needs_human_follow_up:
        return "human"
    if lead.last_contact_date and (now - lead.last_contact_date) <= timedelta(hours=ACTIVE_OUTBOUND_HOURS):
        return "recent_out"
    if lead.sequence_active:
        return "sequence"
    return "active"


def _offer_label(lead):
    """What 'Send offer' will actually do, so the action is clear before clicking."""
    from ghl_integration.pre_pickup import get_pre_pickup_discount
    if get_pre_pickup_discount(lead) > 0:
        return "discount"   # routes to a human, no auto-text
    if lead.segment == "cruise_transfer":
        return "cruise"
    return "urgency"


def _enrich_active(active_leads, now):
    """Attach the active reason + the lead's last reply text (if we have it)."""
    from ghl_integration.models import LeadActivity

    ids = [l.id for l in active_leads]
    latest_meta = {}
    if ids:
        acts = (
            LeadActivity.objects.filter(
                lead_id__in=ids,
                activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
            )
            .order_by("lead_id", "-created_at")
            .values("lead_id", "metadata")
        )
        for a in acts:
            latest_meta.setdefault(a["lead_id"], a["metadata"] or {})

    for lead in active_leads:
        lead.active_reason = _active_reason(lead, now)
        meta = latest_meta.get(lead.id) or {}
        lead.last_reply_text = meta.get("message_body") or meta.get("message_preview")


def _json(request):
    try:
        return json.loads(request.body or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def leads_board_view(request):
    from ghl_integration.models import FollowUpTask
    from ghl_integration.pre_pickup import NUDGE_STEP

    today = timezone.localdate()
    date_str = (request.GET.get("date") or "").strip()
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else today
    except ValueError:
        target = today

    leads = list(
        Lead.objects.filter(pickup_date=target)
        .select_related("vehicle")
        .order_by("-priority", "-created_at")
    )
    lead_ids = [l.id for l in leads]
    nudged_ids = set(
        FollowUpTask.objects.filter(lead_id__in=lead_ids, step_number=NUDGE_STEP)
        .values_list("lead_id", flat=True)
    )

    now = timezone.now()
    buckets = {k: [] for k in BUCKET_ORDER}
    for lead in leads:
        bucket = _classify(lead, now, nudged_ids)
        lead.board_bucket = bucket
        lead.is_nudged = lead.id in nudged_ids
        buckets[bucket].append(lead)

    _enrich_active(buckets["active"], now)
    for lead in buckets["safe"]:
        lead.offer_label = _offer_label(lead)

    open_opps = buckets["safe"] + buckets["active"] + buckets["nudged"]
    potential = sum((l.estimated_price or 0) for l in open_opps)

    sections = [
        {"key": k, "label": BUCKET_LABELS[k], "leads": buckets[k]}
        for k in BUCKET_ORDER
        if buckets[k]
    ]

    context = {
        "target": target,
        "prev_date": (target - timedelta(days=1)).isoformat(),
        "next_date": (target + timedelta(days=1)).isoformat(),
        "today_iso": today.isoformat(),
        "total": len(leads),
        "counts": {k: len(v) for k, v in buckets.items()},
        "open_opportunities": len(open_opps),
        "potential": potential,
        "sections": sections,
    }
    return render(request, "dispatching/leads_board.html", context)


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def lead_board_offer_preview(request):
    """Render the offer text for a lead WITHOUT sending, so staff can review/edit it."""
    from ghl_integration.pre_pickup import (
        PrePickupNudgeEngine, VARIANT_DISCOUNT, get_pre_pickup_discount,
    )

    lead = get_object_or_404(Lead, id=request.GET.get("lead_id"))
    variant, message = PrePickupNudgeEngine().render_preview(lead)
    name = f"{lead.first_name} {lead.last_name}".strip()

    if variant == VARIANT_DISCOUNT:
        disc = get_pre_pickup_discount(lead)
        return JsonResponse({
            "sendable": False, "variant": "discount", "name": name,
            "note": f"{name} has a ${disc:,.0f} discount set. 'Send offer' will NOT text a "
                    f"price — it creates a task for a human to book them with the discount applied.",
        })
    if message is None:
        return JsonResponse({"sendable": False, "variant": variant, "name": name,
                             "note": "No active offer template found for this lead."})
    labels = {"pre_pickup_urgency": "Standard urgency", "pre_pickup_cruise_urgency": "Cruise"}
    return JsonResponse({"sendable": True, "variant": variant, "name": name,
                         "label": labels.get(variant, variant), "message": message})


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
@require_POST
def lead_board_send_nudge(request):
    """Fire a deliberate offer (nudge) now, reusing the engine's hard guards.

    An optional operator-edited ``message`` is sent verbatim (SMS variants).
    """
    from ghl_integration.pre_pickup import PrePickupNudgeEngine

    data = _json(request)
    lead = get_object_or_404(Lead, id=data.get("lead_id"))
    override = (data.get("message") or "").strip() or None
    result = PrePickupNudgeEngine().send_manual(lead, override_message=override)
    head = result.split(":")[0]
    sent = head in ("sent", "routed_to_human", "email_fallback")
    pretty = {
        "sent": "Offer text sent",
        "routed_to_human": "Discount set — routed to a human to book",
        "email_fallback": "Text failed — quote email sent, flagged for human",
        "skipped": "Not sent",
    }.get(head, "Not sent")
    detail = result.split(":", 1)[1] if ":" in result else ""
    return JsonResponse({"success": sent, "result": result,
                         "message": pretty + (f" ({detail})" if not sent and detail else "")})


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
@require_POST
def lead_board_create_task(request):
    """Create a manual human follow-up task for the lead (never auto-texts)."""
    from ops.models import OperationalTask
    from ops.services import create_task

    data = _json(request)
    lead = get_object_or_404(Lead, id=data.get("lead_id"))
    note = (data.get("note") or "").strip()
    due = None
    if lead.pickup_date:
        due = timezone.make_aware(datetime.combine(lead.pickup_date, dt_time(9, 0)))
    task = create_task(
        task_type=OperationalTask.TaskType.MANUAL,
        title=f"Follow up: {lead.first_name} {lead.last_name} (pickup {lead.pickup_date})",
        description=note or "Manual follow-up created from the leads board.",
        priority=OperationalTask.Priority.MEDIUM,
        due_at=due,
        lead=lead,
        created_by=request.user,
    )
    return JsonResponse({
        "success": True,
        "message": "Follow-up task created" if task else "An open follow-up task already exists",
    })


def _ghl_thread(lead):
    """Full SMS thread for a lead, pulled live from GHL (source of truth for
    human-typed outbound replies). Returns [] when the lead was never synced or
    GHL is unreachable, so the caller falls back to local records."""
    if not getattr(lead, "ghl_contact_id", ""):
        return []
    try:
        from ghl_integration.services import GoHighLevelService
        return GoHighLevelService().get_conversation_messages(lead.ghl_contact_id)
    except Exception:
        logger.exception("GHL thread fetch failed for lead %s", lead.id)
        return []


def _system_events(lead):
    """Lifecycle events from our own activity log (sequence started/stopped,
    converted, opt-out…). GHL has no concept of these, so they always come from
    us and are merged into every timeline."""
    from ghl_integration.models import LeadActivity
    reply = LeadActivity.ActivityType.REPLY_RECEIVED
    return [{
        "when": a.created_at, "dir": "event",
        "label": a.get_activity_type_display(), "text": a.description,
    } for a in lead.activities.exclude(activity_type=reply)]


def _local_messages(lead):
    """Message bubbles from our OWN records — shown instantly on open, and the
    fallback when the live GHL thread is unavailable. (We only persist messages
    our automation sent + inbound replies the webhook captured.)"""
    from ghl_integration.models import FollowUpTask, LeadActivity
    reply = LeadActivity.ActivityType.REPLY_RECEIVED
    msgs = []
    for a in lead.activities.filter(activity_type=reply):
        meta = a.metadata or {}
        msgs.append({
            "when": a.created_at, "dir": "in", "label": "Customer replied",
            "text": meta.get("message_body") or meta.get("message_preview") or "",
        })
    for t in lead.follow_up_tasks.filter(status=FollowUpTask.StatusChoices.SENT).exclude(message_body=""):
        msgs.append({
            "when": t.sent_at or t.created_at, "dir": "out",
            "label": "Pre-pickup nudge" if t.step_number == 6 else f"Follow-up step {t.step_number}",
            "text": t.message_body,
        })
    return msgs


def _ghl_messages(thread):
    """Map a live GHL thread (from _ghl_thread) into timeline bubbles."""
    out = []
    for m in thread:
        inbound = m["direction"] == "inbound"
        out.append({
            "when": m["when"], "dir": "in" if inbound else "out",
            "label": "Customer replied" if inbound else "Message sent",
            "text": m["body"],
        })
    return out


def _format_timeline(events):
    """Sort merged events oldest-first and attach display strings."""
    from django.utils.timesince import timesince
    now = timezone.now()
    events.sort(key=lambda e: e["when"] or now)
    return [{
        "dir": e["dir"], "label": e["label"], "text": e["text"] or "",
        "ago": timesince(e["when"], now) + " ago" if e["when"] else "",
    } for e in events]


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def lead_board_detail(request):
    """Phase 1 — lead info + an INSTANT, local-only conversation timeline.

    Deliberately makes NO external call so the panel opens immediately. When the
    lead is synced to GHL we flag ``thread_pending`` so the frontend can pull the
    full live thread (incl. human-typed outbound) from ``lead_board_thread`` and
    swap it in — keeping the open snappy even when GHL is slow or down.
    """
    from django.utils.timesince import timesince
    lead = get_object_or_404(Lead.objects.select_related("vehicle"), id=request.GET.get("lead_id"))
    now = timezone.now()

    timeline = _format_timeline(_system_events(lead) + _local_messages(lead))

    return JsonResponse({
        "name": f"{lead.first_name} {lead.last_name}".strip(),
        "phone": lead.phone or "—",
        "email": lead.email or "—",
        "status": lead.get_status_display(),
        "segment": lead.get_segment_display() if lead.segment else "—",
        "route": f"{lead.pickup_location or '—'} → {lead.dropoff_location or '—'}",
        "vehicle": str(lead.vehicle) if lead.vehicle else "—",
        "price": f"${lead.estimated_price:,.0f}" if lead.estimated_price else "—",
        "pickup_date": lead.pickup_date.isoformat() if lead.pickup_date else "—",
        "flags": {
            "opted_out": lead.sms_opt_out,
            "needs_human": lead.needs_human_follow_up,
            "sequence_active": lead.sequence_active,
            "replied": lead.has_replied,
        },
        "last_contact": (timesince(lead.last_contact_date, now) + " ago") if lead.last_contact_date else "never",
        "last_reply": (timesince(lead.last_reply_at, now) + " ago") if lead.last_reply_at else "—",
        "timeline": timeline,
        "thread_source": "local",
        "thread_pending": bool(getattr(lead, "ghl_contact_id", "")),
        "admin_url": f"/admin/reservations/lead/{lead.id}/change/",
    })


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def lead_board_thread(request):
    """Phase 2 — the FULL conversation: system events merged with the live GHL
    thread (the source of truth for human-typed outbound), falling back to local
    records if GHL is unreachable.

    Fetched asynchronously AFTER the panel is already on screen, so a slow GHL
    call never blocks the open. The GHL fetch is cached ~60s per contact upstream
    (Redis in prod = shared across workers), so repeat opens are instant.
    """
    lead = get_object_or_404(
        Lead.objects.only("id", "ghl_contact_id"), id=request.GET.get("lead_id")
    )
    thread = _ghl_thread(lead)
    messages = _ghl_messages(thread) if thread else _local_messages(lead)
    return JsonResponse({
        "timeline": _format_timeline(_system_events(lead) + messages),
        "thread_source": "ghl" if thread else "local",
    })


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
@require_POST
def lead_board_mark_lost(request):
    """Mark a lead lost (and stop any running automated sequence)."""
    lead = get_object_or_404(Lead, id=_json(request).get("lead_id"))
    if lead.status == Lead.StatusChoices.CONVERTED or lead.converted:
        return JsonResponse({"success": False, "message": "Lead is already booked"})
    lead.status = Lead.StatusChoices.LOST
    lead.save(update_fields=["status"])
    if lead.sequence_active:
        try:
            from ghl_integration.tasks import cancel_lead_sequence
            cancel_lead_sequence(lead.id, reason="manual")
        except Exception:
            pass
    return JsonResponse({"success": True, "message": "Lead marked lost"})
