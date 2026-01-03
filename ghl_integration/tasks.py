"""
Celery tasks for GoHighLevel SMS automation.

These tasks handle syncing leads to GoHighLevel and sending SMS messages.
"""

from celery import shared_task
from django.utils import timezone
from django.db import transaction
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_lead_to_ghl_and_send_sms(self, lead_id):
    """
    Sync a single lead to GHL and send initial SMS.
    
    This task:
    - Gets the lead from database
    - Skips if initial_sms_sent=True or no phone number
    - Creates/updates contact in GHL
    - Generates and sends SMS message
    - Updates lead fields in database
    
    Args:
        lead_id: ID of the Lead to sync
        
    Returns:
        dict: Status and details of the operation
    """
    from reservations.models import Lead
    from .services import GoHighLevelService, get_sms_template
    
    try:
        lead = Lead.objects.get(id=lead_id)
        
        # Skip if already sent
        if lead.initial_sms_sent:
            logger.info(f"Lead #{lead_id} already has SMS sent, skipping")
            return {"status": "skipped", "reason": "already_sent", "lead_id": lead_id}
        
        # Skip if no phone
        if not lead.phone:
            logger.warning(f"Lead #{lead_id} has no phone number")
            return {"status": "skipped", "reason": "no_phone", "lead_id": lead_id}
        
        service = GoHighLevelService()
        
        # Step 1: Create/update contact in GHL
        contact_id = service.create_or_update_contact(lead)
        
        if not contact_id:
            logger.error(f"Failed to create/update GHL contact for lead #{lead_id}")
            raise Exception("Failed to create/update GHL contact")
        
        # Step 2: Generate and send SMS
        message = get_sms_template(lead)
        sms_sent = service.send_sms(contact_id, message)
        
        # Only proceed if SMS was successfully sent
        if not sms_sent:
            logger.error(f"Failed to send SMS to contact {contact_id} for lead #{lead_id}")
            raise Exception("Failed to send SMS")
        
        # Step 3: Update Django lead within transaction
        # Only update status to "contacted" if SMS was successfully sent
        with transaction.atomic():
            lead.ghl_contact_id = contact_id
            lead.ghl_synced_at = timezone.now()
            lead.initial_sms_sent = True
            lead.initial_sms_sent_at = timezone.now()
            lead.status = Lead.StatusChoices.CONTACTED  # Mark as contacted - SMS sent successfully
            lead.contact_attempts = (lead.contact_attempts or 0) + 1
            lead.last_contact_date = timezone.now()
            lead.save(update_fields=[
                'ghl_contact_id', 
                'ghl_synced_at', 
                'initial_sms_sent',
                'initial_sms_sent_at', 
                'status', 
                'contact_attempts', 
                'last_contact_date'
            ])
        
        logger.info(f"Successfully synced Lead #{lead_id} to GHL, sent SMS, and marked as CONTACTED (contact_id: {contact_id})")
        
        return {
            "status": "success",
            "lead_id": lead_id,
            "ghl_contact_id": contact_id,
            "sms_sent": sms_sent
        }
        
    except Lead.DoesNotExist:
        logger.error(f"Lead #{lead_id} not found")
        return {"status": "error", "reason": "lead_not_found", "lead_id": lead_id}
    except Exception as e:
        logger.error(f"Error syncing Lead #{lead_id} to GHL: {e}", exc_info=True)
        # Retry with exponential backoff
        raise self.retry(exc=e)


@shared_task
def batch_send_unsent_leads():
    """
    Batch task to find all leads without SMS sent and queue them for sending.
    
    This task:
    - Queries leads where initial_sms_sent=False, has phone number,
      status in ['new', 'contacted'], and converted=False
    - Limits to 50 leads per batch
    - Queues each lead for individual processing
    
    Returns:
        dict: Count of queued leads
    """
    from reservations.models import Lead
    
    # Get leads that need SMS
    unsent_leads = Lead.objects.filter(
        initial_sms_sent=False,
        phone__isnull=False,
        status__in=[Lead.StatusChoices.NEW, Lead.StatusChoices.CONTACTED],
        converted=False
    ).exclude(
        phone=""
    ).values_list('id', flat=True)[:50]  # Limit batch size
    
    count = 0
    for lead_id in unsent_leads:
        sync_lead_to_ghl_and_send_sms.delay(lead_id)
        count += 1
    
    logger.info(f"Queued {count} leads for GHL sync and SMS")
    return {"queued": count}


@shared_task
def sync_lead_to_ghl_without_sms(lead_id):
    """
    Sync a single lead to GHL without sending SMS.
    
    Useful for importing existing leads or syncing contact information
    without triggering an SMS message.
    
    Args:
        lead_id: ID of the Lead to sync
        
    Returns:
        dict: Status and details of the operation
    """
    from reservations.models import Lead
    from .services import GoHighLevelService
    
    try:
        lead = Lead.objects.get(id=lead_id)
        
        # Skip if no phone
        if not lead.phone:
            logger.warning(f"Lead #{lead_id} has no phone number")
            return {"status": "skipped", "reason": "no_phone", "lead_id": lead_id}
        
        service = GoHighLevelService()
        
        # Create/update contact in GHL
        contact_id = service.create_or_update_contact(lead)
        
        if not contact_id:
            logger.error(f"Failed to create/update GHL contact for lead #{lead_id}")
            return {"status": "error", "reason": "failed_to_sync", "lead_id": lead_id}
        
        # Update Django lead within transaction
        with transaction.atomic():
            lead.ghl_contact_id = contact_id
            lead.ghl_synced_at = timezone.now()
            lead.save(update_fields=['ghl_contact_id', 'ghl_synced_at'])
        
        logger.info(f"Successfully synced Lead #{lead_id} to GHL without SMS (contact_id: {contact_id})")
        
        return {
            "status": "success",
            "lead_id": lead_id,
            "ghl_contact_id": contact_id
        }
        
    except Lead.DoesNotExist:
        logger.error(f"Lead #{lead_id} not found")
        return {"status": "error", "reason": "lead_not_found", "lead_id": lead_id}
    except Exception as e:
        logger.error(f"Error syncing Lead #{lead_id} to GHL: {e}", exc_info=True)
        return {"status": "error", "reason": "internal_error", "lead_id": lead_id}
