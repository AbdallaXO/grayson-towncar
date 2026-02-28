"""
Views for GoHighLevel integration, including webhook endpoints.
"""

import json
import logging
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db import transaction
from reservations.models import Lead
from reservations.utils import send_ntfy_notification

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def ghl_webhook(request):
    """
    Webhook endpoint to receive notifications from GoHighLevel.
    
    Handles incoming SMS messages and updates lead status accordingly.
    
    Expected event types:
    - 'InboundMessage'
    - 'message.received'
    - 'SMS'
    """
    try:
        # Parse JSON payload
        payload = json.loads(request.body)
        logger.info(f"GHL Webhook received: {payload}")
        
        # Extract event type and normalize to lowercase
        event_type = (payload.get('type') or payload.get('event') or payload.get('eventType', '')).lower()

        # Only handle inbound (reply) messages — do NOT match outbound events
        is_inbound_message = (
            event_type == 'inboundmessage' or
            event_type == 'message.received' or
            'inbound' in event_type
        )
        
        if not is_inbound_message:
            logger.info(f"Webhook event type '{event_type}' not handled, skipping")
            return JsonResponse({"status": "ignored", "reason": "event_type_not_handled"})
        
        # Extract contact ID and message body
        # GHL webhook structure can vary, try multiple possible locations
        contact_id = (
            payload.get('contactId') or
            payload.get('contact', {}).get('id') or
            payload.get('data', {}).get('contactId') or
            payload.get('message', {}).get('contactId')
        )
        
        message_body = (
            payload.get('body') or
            payload.get('message', {}).get('body') or
            payload.get('text') or
            payload.get('message', {}).get('text') or
            payload.get('content') or
            payload.get('message', {}).get('content')
        )
        
        if not contact_id:
            logger.warning(f"No contactId found in webhook payload: {payload}")
            return JsonResponse({"status": "error", "reason": "missing_contact_id"}, status=400)
        
        if not message_body:
            logger.warning(f"No message body found in webhook payload: {payload}")
            return JsonResponse({"status": "error", "reason": "missing_message_body"}, status=400)
        
        # Find Lead by GHL contact ID
        try:
            lead = Lead.objects.get(ghl_contact_id=contact_id)
        except Lead.DoesNotExist:
            logger.warning(f"Lead not found for GHL contact ID: {contact_id}")
            return JsonResponse({"status": "error", "reason": "lead_not_found"}, status=404)
        except Lead.MultipleObjectsReturned:
            logger.error(f"Multiple leads found for GHL contact ID: {contact_id}")
            # Get the most recent one
            lead = Lead.objects.filter(ghl_contact_id=contact_id).order_by('-created_at').first()
        
        # Update lead within transaction
        with transaction.atomic():
            lead.has_replied = True
            lead.last_reply_at = timezone.now()
            
            # Upgrade priority
            priority_upgrade_map = {
                Lead.PriorityChoices.LOW: Lead.PriorityChoices.MEDIUM,
                Lead.PriorityChoices.MEDIUM: Lead.PriorityChoices.HIGH,
                Lead.PriorityChoices.HIGH: Lead.PriorityChoices.URGENT,
                Lead.PriorityChoices.URGENT: Lead.PriorityChoices.URGENT,  # Stay at urgent
            }
            
            current_priority = lead.priority
            new_priority = priority_upgrade_map.get(current_priority, Lead.PriorityChoices.HIGH)
            
            if new_priority != current_priority:
                lead.priority = new_priority
                logger.info(f"Upgraded lead #{lead.id} priority from {current_priority} to {new_priority}")
            
            # Update status to 'interested' if it's still 'new' or 'contacted'
            if lead.status in [Lead.StatusChoices.NEW, Lead.StatusChoices.CONTACTED]:
                lead.status = Lead.StatusChoices.INTERESTED
                logger.info(f"Updated lead #{lead.id} status to 'interested'")
            
            lead.save(update_fields=[
                'has_replied',
                'last_reply_at',
                'priority',
                'status'
            ])
        
        # Send notification
        lead_name = lead.get_full_name
        title = f"🔥 Lead Replied: {lead_name}"
        notification_message = f"""
Lead replied via SMS!

Lead: {lead_name}
Phone: {lead.phone or 'N/A'}
Message: {message_body}

Lead ID: #{lead.id}
Priority: {lead.priority.title()}
Status: {lead.status.title()}
        """.strip()
        
        send_ntfy_notification(
            title=title,
            message=notification_message,
            priority="urgent",
            tags=["fire", "reply", "sms", "lead"]
        )
        
        logger.info(f"Successfully processed GHL webhook for lead #{lead.id} (contact_id: {contact_id})")
        
        return JsonResponse({
            "status": "success",
            "lead_id": lead.id,
            "contact_id": contact_id
        })
        
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in GHL webhook: {e}")
        return JsonResponse({"status": "error", "reason": "invalid_json"}, status=400)
    except Exception as e:
        logger.error(f"Error processing GHL webhook: {e}", exc_info=True)
        return JsonResponse({"status": "error", "reason": "internal_error"}, status=500)
