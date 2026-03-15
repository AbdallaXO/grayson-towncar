"""
Views for GoHighLevel integration, including webhook endpoints.
"""

import json
import logging
import re
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from reservations.models import Lead
from reservations.utils import send_ntfy_notification

logger = logging.getLogger(__name__)


def _normalize_phone_last10(phone):
    """Extract last 10 digits from a phone string for matching."""
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    return digits[-10:] if len(digits) >= 10 else None


def _find_leads_for_webhook(contact_id, phone=None):
    """
    Find ALL leads matching a GHL contact ID or phone number.
    Returns (primary_lead, all_matching_leads).

    When a customer submits multiple quote forms (round-trip, different dates),
    they end up with multiple Lead records sharing the same phone number.
    We need to stop sequences on ALL of them when they reply.
    """
    leads = []
    seen_ids = set()

    # 1. Find by GHL contact ID (most direct match)
    if contact_id:
        ghl_leads = Lead.objects.filter(ghl_contact_id=contact_id)
        for lead in ghl_leads:
            if lead.id not in seen_ids:
                leads.append(lead)
                seen_ids.add(lead.id)

    # 2. Also find by phone number (catches sibling leads with different/no contact ID)
    if leads:
        phone = phone or leads[0].phone
    if phone:
        last10 = _normalize_phone_last10(phone)
        if last10:
            # Use last 4 digits as a fast DB filter
            phone_candidates = Lead.objects.filter(
                phone__contains=last10[-4:],
            ).exclude(id__in=seen_ids).exclude(phone__isnull=True).exclude(phone="")
            for candidate in phone_candidates:
                cand_last10 = _normalize_phone_last10(candidate.phone)
                if cand_last10 == last10 and candidate.id not in seen_ids:
                    leads.append(candidate)
                    seen_ids.add(candidate.id)

    primary = leads[0] if leads else None
    return primary, leads


@csrf_exempt
@require_POST
def ghl_webhook(request):
    """
    Webhook endpoint to receive notifications from GoHighLevel.

    Handles incoming SMS messages and updates lead status accordingly.
    Finds ALL leads matching the contact (by GHL ID and phone) and cancels
    sequences on all of them — prevents continued texting when a customer
    has multiple leads (e.g. round-trip quotes).
    """
    try:
        # Parse JSON payload
        payload = json.loads(request.body)
        logger.info(f"GHL Webhook received: {json.dumps(payload, default=str)[:1000]}")

        # GHL workflow custom data may be nested under 'customData' key
        # or sent as top-level fields depending on workflow config
        custom_data = payload.get('customData', {})

        # Extract event type — check top-level then customData, normalize to lowercase
        event_type = (
            payload.get('type') or payload.get('event') or payload.get('eventType') or
            custom_data.get('type') or custom_data.get('event') or custom_data.get('eventType') or ''
        ).lower().strip()

        # Accept the webhook if it's any kind of inbound/reply event,
        # OR if it has our custom type=InboundMessage from the workflow.
        # Be permissive: if we have a contactId, process it.
        is_inbound_message = (
            event_type == 'inboundmessage' or
            event_type == 'message.received' or
            event_type == 'customer replied' or
            'inbound' in event_type or
            'replied' in event_type or
            'reply' in event_type
        )

        # Extract contact ID — check top-level then customData
        contact_id = (
            payload.get('contactId') or payload.get('contact_id') or
            payload.get('contact', {}).get('id') if isinstance(payload.get('contact'), dict) else None
        ) or (
            custom_data.get('contactId') or custom_data.get('contact_id')
        )

        # Extract phone number from payload (fallback for contact matching)
        phone = (
            payload.get('phone') or payload.get('from') or
            custom_data.get('phone') or custom_data.get('from') or
            (payload.get('contact', {}).get('phone') if isinstance(payload.get('contact'), dict) else None)
        )

        # Extract message body — check top-level then customData
        message_body = (
            payload.get('body') or payload.get('text') or payload.get('content') or
            custom_data.get('body') or custom_data.get('text') or custom_data.get('content') or
            (payload.get('message', {}).get('body') if isinstance(payload.get('message'), dict) else None)
        )

        # If we have a contactId, always process (even if event type doesn't match perfectly)
        # This handles cases where GHL sends unexpected event type formats
        if not is_inbound_message and not contact_id:
            logger.info(f"Webhook event type '{event_type}' not handled and no contactId, skipping")
            return JsonResponse({"status": "ignored", "reason": "event_type_not_handled"})

        if not contact_id and not phone:
            logger.warning(f"No contactId or phone found in webhook payload: {payload}")
            return JsonResponse({"status": "error", "reason": "missing_contact_id"}, status=400)

        if not message_body:
            logger.info("No message body in webhook payload — proceeding anyway")

        # Find ALL matching leads (by GHL contact ID and phone number)
        primary_lead, all_leads = _find_leads_for_webhook(contact_id, phone)

        if not primary_lead:
            logger.warning(f"No leads found for GHL contact ID: {contact_id}, phone: {phone}")
            return JsonResponse({"status": "error", "reason": "lead_not_found"}, status=404)

        lead_ids_updated = []

        # Update ALL matching leads
        for lead in all_leads:
            with transaction.atomic():
                # Re-fetch to avoid stale data
                lead.refresh_from_db()

                lead.has_replied = True
                lead.last_reply_at = timezone.now()
                lead.needs_human_follow_up = True

                # Upgrade priority
                priority_upgrade_map = {
                    Lead.PriorityChoices.LOW: Lead.PriorityChoices.MEDIUM,
                    Lead.PriorityChoices.MEDIUM: Lead.PriorityChoices.HIGH,
                    Lead.PriorityChoices.HIGH: Lead.PriorityChoices.URGENT,
                    Lead.PriorityChoices.URGENT: Lead.PriorityChoices.URGENT,
                }

                current_priority = lead.priority
                new_priority = priority_upgrade_map.get(current_priority, Lead.PriorityChoices.HIGH)
                if new_priority != current_priority:
                    lead.priority = new_priority

                # Update status to 'interested' if still new or contacted
                if lead.status in [Lead.StatusChoices.NEW, Lead.StatusChoices.CONTACTED]:
                    lead.status = Lead.StatusChoices.INTERESTED

                lead.save(update_fields=[
                    'has_replied',
                    'last_reply_at',
                    'priority',
                    'status',
                    'needs_human_follow_up',
                ])

            # Cancel any active follow-up sequence for this lead
            if lead.sequence_active:
                try:
                    from ghl_integration.tasks import cancel_lead_sequence
                    cancel_lead_sequence(lead.id, reason="replied")
                except Exception:
                    logger.warning(f"Failed to cancel sequence for lead #{lead.id}")

            lead_ids_updated.append(lead.id)

        # Apply lifecycle tags for primary lead (best-effort)
        if primary_lead.ghl_contact_id:
            try:
                from ghl_integration.runner import run_in_background as _run_bg
                from ghl_integration.services import GoHighLevelService as _GHL

                def _apply_reply_tags(cid=primary_lead.ghl_contact_id, lid=primary_lead.id):
                    fresh = Lead.objects.get(id=lid)
                    _GHL().apply_lifecycle_tags(cid, fresh, "replied")

                _run_bg(_apply_reply_tags)
            except Exception:
                logger.warning(f"Failed to queue reply tags for lead #{primary_lead.id}")

        # Log activity for the primary lead
        try:
            from ghl_integration.models import LeadActivity
            sibling_note = ""
            if len(all_leads) > 1:
                sibling_ids = [l.id for l in all_leads if l.id != primary_lead.id]
                sibling_note = f" (also updated sibling leads: {sibling_ids})"
            LeadActivity.objects.create(
                lead=primary_lead,
                activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
                description=f"SMS reply received: {(message_body or '')[:100]}{sibling_note}",
                metadata={
                    "contact_id": contact_id,
                    "message_preview": (message_body or "")[:200],
                    "all_lead_ids": lead_ids_updated,
                },
            )
        except Exception:
            logger.warning(f"Failed to create reply activity for lead #{primary_lead.id}")

        # Send notification
        lead_name = primary_lead.get_full_name
        title = f"Lead Replied: {lead_name}"
        notification_message = f"""Lead replied via SMS!

Lead: {lead_name}
Phone: {primary_lead.phone or 'N/A'}
Message: {message_body or '(not captured)'}

Lead ID: #{primary_lead.id}
Leads updated: {lead_ids_updated}
Priority: {primary_lead.priority.title()}
Status: {primary_lead.status.title()}""".strip()

        send_ntfy_notification(
            title=title,
            message=notification_message,
            priority="urgent",
            tags=["fire", "reply", "sms", "lead"]
        )

        logger.info(
            f"Processed GHL webhook: updated {len(lead_ids_updated)} lead(s) "
            f"(IDs: {lead_ids_updated}, contact_id: {contact_id})"
        )

        return JsonResponse({
            "status": "success",
            "lead_ids": lead_ids_updated,
            "contact_id": contact_id
        })

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in GHL webhook: {e}")
        return JsonResponse({"status": "error", "reason": "invalid_json"}, status=400)
    except Exception as e:
        logger.error(f"Error processing GHL webhook: {e}", exc_info=True)
        return JsonResponse({"status": "error", "reason": "internal_error"}, status=500)
