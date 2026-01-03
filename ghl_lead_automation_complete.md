z`# Complete GoHighLevel Lead SMS Automation
## With Two-Way Django Sync

This implementation gives you:
- ✅ Automatic SMS to new leads (or batch trigger)
- ✅ Django Lead marked as "contacted" when SMS sent
- ✅ Django notified when customer replies
- ✅ Polished GHL Conversations inbox for managing replies
- ✅ Mobile push notifications

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         YOUR SYSTEM                                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   DJANGO                              GOHIGHLEVEL                    │
│   ┌──────────────┐                   ┌──────────────┐               │
│   │              │   1. Create       │              │               │
│   │  Lead Model  │ ──────────────►   │   Contact    │               │
│   │              │   (API call)      │              │               │
│   │  - status    │                   │  - custom    │               │
│   │  - ghl_id    │   4. Update       │    fields    │               │
│   │  - sms_sent  │ ◄──────────────   │              │               │
│   │              │   (webhook)       └──────┬───────┘               │
│   └──────────────┘                          │                        │
│                                             │ 2. Workflow            │
│                                             │    sends SMS           │
│                                             ▼                        │
│                                    ┌──────────────┐                  │
│                                    │  SMS Sent    │                  │
│                                    │  to Customer │                  │
│                                    └──────┬───────┘                  │
│                                           │                          │
│                                           │ 3. Customer              │
│                                           │    replies               │
│                                           ▼                          │
│   ┌──────────────┐                ┌──────────────┐                  │
│   │   Django     │   5. Notify    │ Conversations│                  │
│   │   Webhook    │ ◄────────────  │    Inbox     │                  │
│   │   Endpoint   │   (webhook)    │  (you reply) │                  │
│   └──────────────┘                └──────────────┘                  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Step 1: Update Your Django Lead Model

Add fields to track GHL sync status:

```python
# reservations/models.py - Add to Lead model

class Lead(models.Model):
    # ... existing fields ...
    
    # GHL Integration Fields
    ghl_contact_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    ghl_synced_at = models.DateTimeField(null=True, blank=True)
    initial_sms_sent = models.BooleanField(default=False)
    initial_sms_sent_at = models.DateTimeField(null=True, blank=True)
    last_reply_at = models.DateTimeField(null=True, blank=True)
    has_replied = models.BooleanField(default=False)
    
    class Meta:
        # ... existing meta ...
        indexes = [
            # ... existing indexes ...
            models.Index(fields=['ghl_contact_id']),
            models.Index(fields=['initial_sms_sent', 'created_at']),
        ]
```

Run migration:
```bash
python manage.py makemigrations reservations
python manage.py migrate
```

---

## Step 2: Create GHL Service Class

```python
# ghl_integration/services.py

import requests
import logging
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class GoHighLevelService:
    """
    Service class for GoHighLevel API interactions.
    Uses Location API (sub-account level).
    """
    
    BASE_URL = "https://services.leadconnectorhq.com"
    
    def __init__(self):
        self.api_key = settings.GHL_API_KEY  # Location API key
        self.location_id = settings.GHL_LOCATION_ID
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Version": "2021-07-28"
        }
    
    def create_or_update_contact(self, lead):
        """
        Create or update a contact in GHL from a Django Lead.
        Returns the GHL contact ID.
        """
        endpoint = f"{self.BASE_URL}/contacts/"
        
        # Format phone number (GHL expects +1XXXXXXXXXX)
        phone = self._format_phone(lead.phone)
        
        # Format pickup date for display
        pickup_date_str = ""
        if lead.pickup_date:
            pickup_date_str = lead.pickup_date.strftime("%B %d, %Y")
        
        payload = {
            "locationId": self.location_id,
            "firstName": lead.first_name or "",
            "lastName": lead.last_name or "",
            "phone": phone,
            "email": lead.email or "",
            "source": "Website Lead Form",
            "tags": ["Django Lead", "Needs SMS"],
            "customFields": [
                {"key": "pickup_location", "value": lead.pickup_location or ""},
                {"key": "dropoff_location", "value": lead.dropoff_location or ""},
                {"key": "pickup_date", "value": pickup_date_str},
                {"key": "django_lead_id", "value": str(lead.id)},
                {"key": "estimated_price", "value": str(lead.estimated_price) if lead.estimated_price else ""},
                {"key": "vehicle_type", "value": lead.vehicle.vehicle_type if lead.vehicle else ""},
            ]
        }
        
        try:
            # First, check if contact exists by phone
            existing = self.find_contact_by_phone(phone)
            
            if existing:
                # Update existing contact
                contact_id = existing['id']
                response = requests.put(
                    f"{endpoint}{contact_id}",
                    headers=self.headers,
                    json=payload,
                    timeout=30
                )
            else:
                # Create new contact
                response = requests.post(
                    endpoint,
                    headers=self.headers,
                    json=payload,
                    timeout=30
                )
            
            response.raise_for_status()
            data = response.json()
            contact_id = data.get('contact', {}).get('id') or data.get('id')
            
            logger.info(f"GHL contact {'updated' if existing else 'created'}: {contact_id} for Lead #{lead.id}")
            return contact_id
            
        except requests.exceptions.RequestException as e:
            logger.error(f"GHL API error for Lead #{lead.id}: {e}")
            raise
    
    def find_contact_by_phone(self, phone):
        """Find a contact by phone number."""
        endpoint = f"{self.BASE_URL}/contacts/search"
        
        payload = {
            "locationId": self.location_id,
            "phone": phone
        }
        
        try:
            response = requests.post(
                endpoint,
                headers=self.headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            contacts = data.get('contacts', [])
            return contacts[0] if contacts else None
        except:
            return None
    
    def send_sms(self, contact_id, message):
        """
        Send an SMS to a contact.
        Returns message ID if successful.
        """
        endpoint = f"{self.BASE_URL}/conversations/messages"
        
        payload = {
            "type": "SMS",
            "contactId": contact_id,
            "message": message
        }
        
        try:
            response = requests.post(
                endpoint,
                headers=self.headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            message_id = data.get('messageId') or data.get('id')
            
            logger.info(f"SMS sent via GHL to contact {contact_id}: {message_id}")
            return message_id
            
        except requests.exceptions.RequestException as e:
            logger.error(f"GHL SMS error for contact {contact_id}: {e}")
            raise
    
    def add_tag(self, contact_id, tag):
        """Add a tag to a contact."""
        endpoint = f"{self.BASE_URL}/contacts/{contact_id}/tags"
        
        payload = {"tags": [tag]}
        
        try:
            response = requests.post(
                endpoint,
                headers=self.headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            return True
        except:
            return False
    
    def remove_tag(self, contact_id, tag):
        """Remove a tag from a contact."""
        endpoint = f"{self.BASE_URL}/contacts/{contact_id}/tags"
        
        try:
            response = requests.delete(
                endpoint,
                headers=self.headers,
                json={"tags": [tag]},
                timeout=30
            )
            response.raise_for_status()
            return True
        except:
            return False
    
    @staticmethod
    def _format_phone(phone):
        """Format phone to E.164 (+1XXXXXXXXXX)."""
        import re
        if not phone:
            return ""
        digits = re.sub(r'\D', '', phone)
        if len(digits) == 10:
            return f"+1{digits}"
        elif len(digits) == 11 and digits[0] == '1':
            return f"+{digits}"
        return phone


def get_sms_template(lead):
    """
    Generate the SMS message for a lead using template.
    """
    # Format pickup date nicely
    pickup_date_str = "your upcoming trip"
    if lead.pickup_date:
        pickup_date_str = lead.pickup_date.strftime("%B %d")
    
    # Build message
    message = (
        f"Hey {lead.first_name or 'there'}, this is Grayson Towncar. "
        f"Do you still need transportation from {lead.pickup_location or 'your pickup location'} "
        f"to {lead.dropoff_location or 'your destination'} on {pickup_date_str}? "
        f"Reply YES to confirm or call 407-212-7190!"
    )
    
    return message
```

---

## Step 3: Create Lead Sync Task

```python
# ghl_integration/tasks.py

from celery import shared_task
from django.utils import timezone
from django.db import transaction
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_lead_to_ghl_and_send_sms(self, lead_id):
    """
    Sync a single lead to GHL and send initial SMS.
    Called automatically on lead creation or manually.
    """
    from reservations.models import Lead
    from .services import GoHighLevelService, get_sms_template
    
    try:
        lead = Lead.objects.get(id=lead_id)
        
        # Skip if already sent
        if lead.initial_sms_sent:
            logger.info(f"Lead #{lead_id} already has SMS sent, skipping")
            return {"status": "skipped", "reason": "already_sent"}
        
        # Skip if no phone
        if not lead.phone:
            logger.warning(f"Lead #{lead_id} has no phone number")
            return {"status": "skipped", "reason": "no_phone"}
        
        service = GoHighLevelService()
        
        # Step 1: Create/update contact in GHL
        contact_id = service.create_or_update_contact(lead)
        
        # Step 2: Generate and send SMS
        message = get_sms_template(lead)
        message_id = service.send_sms(contact_id, message)
        
        # Step 3: Update Django lead
        with transaction.atomic():
            lead.ghl_contact_id = contact_id
            lead.ghl_synced_at = timezone.now()
            lead.initial_sms_sent = True
            lead.initial_sms_sent_at = timezone.now()
            lead.status = "contacted"  # Mark as contacted!
            lead.contact_attempts = (lead.contact_attempts or 0) + 1
            lead.last_contact_date = timezone.now()
            lead.save(update_fields=[
                'ghl_contact_id', 'ghl_synced_at', 'initial_sms_sent',
                'initial_sms_sent_at', 'status', 'contact_attempts', 'last_contact_date'
            ])
        
        # Step 4: Update tags in GHL
        service.remove_tag(contact_id, "Needs SMS")
        service.add_tag(contact_id, "SMS Sent")
        
        logger.info(f"Successfully synced Lead #{lead_id} to GHL and sent SMS")
        
        return {
            "status": "success",
            "lead_id": lead_id,
            "ghl_contact_id": contact_id,
            "message_id": message_id
        }
        
    except Exception as e:
        logger.error(f"Error syncing Lead #{lead_id} to GHL: {e}")
        # Retry with exponential backoff
        raise self.retry(exc=e)


@shared_task
def batch_send_unsent_leads():
    """
    Find all leads without SMS sent and sync them.
    Can be scheduled (e.g., every hour) or triggered manually.
    """
    from reservations.models import Lead
    
    # Get leads that need SMS
    unsent_leads = Lead.objects.filter(
        initial_sms_sent=False,
        phone__isnull=False,
        status__in=['new', 'contacted'],  # Don't send to converted/lost
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
def sync_single_lead_no_sms(lead_id):
    """
    Sync lead to GHL without sending SMS.
    Useful for importing existing leads.
    """
    from reservations.models import Lead
    from .services import GoHighLevelService
    
    try:
        lead = Lead.objects.get(id=lead_id)
        
        if not lead.phone:
            return {"status": "skipped", "reason": "no_phone"}
        
        service = GoHighLevelService()
        contact_id = service.create_or_update_contact(lead)
        
        lead.ghl_contact_id = contact_id
        lead.ghl_synced_at = timezone.now()
        lead.save(update_fields=['ghl_contact_id', 'ghl_synced_at'])
        
        return {"status": "success", "ghl_contact_id": contact_id}
        
    except Exception as e:
        logger.error(f"Error syncing Lead #{lead_id}: {e}")
        raise
```

---

## Step 4: Create Webhook Endpoint for GHL Callbacks

```python
# ghl_integration/views.py

import json
import logging
import hmac
import hashlib
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.conf import settings

logger = logging.getLogger(__name__)


def verify_ghl_signature(request):
    """Verify webhook signature from GHL (if configured)."""
    # GHL webhook signature verification
    # Check their docs for current signature method
    return True  # Implement based on GHL's current signing method


@csrf_exempt
@require_POST
def ghl_webhook(request):
    """
    Receive webhooks from GoHighLevel for:
    - Message received (customer reply)
    - Message status updates
    - Contact updates
    """
    try:
        # Verify signature
        if not verify_ghl_signature(request):
            logger.warning("Invalid GHL webhook signature")
            return HttpResponse(status=401)
        
        payload = json.loads(request.body)
        event_type = payload.get('type') or payload.get('event')
        
        logger.info(f"GHL Webhook received: {event_type}")
        
        # Handle different event types
        if event_type in ['InboundMessage', 'message.received', 'SMS']:
            handle_inbound_message(payload)
        
        elif event_type in ['OutboundMessage', 'message.sent']:
            handle_outbound_message(payload)
        
        elif event_type in ['ContactUpdate', 'contact.updated']:
            handle_contact_update(payload)
        
        return JsonResponse({"status": "ok"})
        
    except json.JSONDecodeError:
        logger.error("Invalid JSON in GHL webhook")
        return HttpResponse(status=400)
    except Exception as e:
        logger.error(f"Error processing GHL webhook: {e}")
        return HttpResponse(status=500)


def handle_inbound_message(payload):
    """
    Handle incoming SMS from customer.
    Update Django lead with reply info.
    """
    from reservations.models import Lead
    from reservations.utils import send_ntfy_notification
    
    contact_id = payload.get('contactId') or payload.get('contact_id')
    message_body = payload.get('body') or payload.get('message', '')
    
    if not contact_id:
        logger.warning("No contact_id in inbound message webhook")
        return
    
    # Find the lead by GHL contact ID
    try:
        lead = Lead.objects.get(ghl_contact_id=contact_id)
        
        # Update lead with reply info
        lead.has_replied = True
        lead.last_reply_at = timezone.now()
        
        # Upgrade status if still new
        if lead.status == 'new':
            lead.status = 'contacted'
        
        # Upgrade priority since they replied
        if lead.priority == 'low':
            lead.priority = 'medium'
        elif lead.priority == 'medium':
            lead.priority = 'high'
        
        lead.save(update_fields=['has_replied', 'last_reply_at', 'status', 'priority'])
        
        logger.info(f"Lead #{lead.id} replied via SMS")
        
        # Send NTFY notification for hot lead!
        send_ntfy_notification(
            title=f"🔥 Lead Replied: {lead.get_full_name}",
            message=f"Phone: {lead.phone}\n\nMessage: {message_body[:200]}",
            priority="high",
            tags=["speech_balloon", "fire"]
        )
        
    except Lead.DoesNotExist:
        logger.warning(f"No lead found for GHL contact {contact_id}")
    except Lead.MultipleObjectsReturned:
        logger.error(f"Multiple leads found for GHL contact {contact_id}")


def handle_outbound_message(payload):
    """
    Handle confirmation of sent SMS.
    Could be used for delivery status tracking.
    """
    contact_id = payload.get('contactId') or payload.get('contact_id')
    status = payload.get('status', '')
    
    logger.info(f"Outbound SMS status for {contact_id}: {status}")
    # Could update lead with delivery confirmation


def handle_contact_update(payload):
    """
    Handle contact updates from GHL.
    Sync relevant changes back to Django.
    """
    contact_id = payload.get('contactId') or payload.get('id')
    
    # Could sync tags, status changes, etc.
    logger.info(f"Contact updated in GHL: {contact_id}")
```

---

## Step 5: URL Configuration

```python
# ghl_integration/urls.py

from django.urls import path
from . import views

urlpatterns = [
    path('webhook/', views.ghl_webhook, name='ghl_webhook'),
]

# In business/urls.py, add:
# path('ghl/', include('ghl_integration.urls')),
```

---

## Step 6: Settings Configuration

```python
# business/settings.py - Add these settings

# GoHighLevel Configuration
GHL_API_KEY = os.environ.get('GHL_API_KEY', '')  # Location API key
GHL_LOCATION_ID = os.environ.get('GHL_LOCATION_ID', '')  # Your sub-account ID
GHL_WEBHOOK_SECRET = os.environ.get('GHL_WEBHOOK_SECRET', '')  # Optional signing secret

# Add to INSTALLED_APPS
INSTALLED_APPS = [
    # ... existing apps ...
    'ghl_integration',
]

# Celery Beat Schedule - Add hourly batch
CELERY_BEAT_SCHEDULE = {
    # ... existing tasks ...
    
    'batch-send-lead-sms': {
        'task': 'ghl_integration.tasks.batch_send_unsent_leads',
        'schedule': crontab(minute=0),  # Every hour on the hour
    },
}
```

---

## Step 7: Admin Integration

```python
# ghl_integration/admin.py (or add to reservations/admin.py)

from django.contrib import admin
from django.contrib import messages
from reservations.models import Lead


# Add to LeadAdmin
class LeadAdmin(admin.ModelAdmin):
    # ... existing configuration ...
    
    list_display = [
        # ... existing fields ...
        'initial_sms_sent',
        'has_replied',
        'ghl_synced',
    ]
    
    list_filter = [
        # ... existing filters ...
        'initial_sms_sent',
        'has_replied',
    ]
    
    actions = [
        'send_sms_to_selected',
        'sync_to_ghl_without_sms',
    ]
    
    def ghl_synced(self, obj):
        if obj.ghl_contact_id:
            return "✅"
        return "❌"
    ghl_synced.short_description = "GHL Synced"
    
    @admin.action(description="📱 Send SMS to selected leads (via GHL)")
    def send_sms_to_selected(self, request, queryset):
        from ghl_integration.tasks import sync_lead_to_ghl_and_send_sms
        
        # Filter to only unsent leads
        unsent = queryset.filter(initial_sms_sent=False).exclude(phone="")
        count = 0
        
        for lead in unsent:
            sync_lead_to_ghl_and_send_sms.delay(lead.id)
            count += 1
        
        if count:
            messages.success(request, f"Queued {count} leads for SMS sending")
        else:
            messages.warning(request, "No eligible leads selected (already sent or no phone)")
    
    @admin.action(description="🔄 Sync to GHL (no SMS)")
    def sync_to_ghl_without_sms(self, request, queryset):
        from ghl_integration.tasks import sync_single_lead_no_sms
        
        count = 0
        for lead in queryset.exclude(phone=""):
            sync_single_lead_no_sms.delay(lead.id)
            count += 1
        
        messages.success(request, f"Queued {count} leads for GHL sync")
```

---

## Step 8: GHL Workflow Setup

In GoHighLevel, create a workflow to handle webhook callbacks:

### Workflow: "Notify Django on Reply"

1. **Trigger**: "Customer Replied"
2. **Action**: "Webhook" → POST to `https://yourdomain.com/ghl/webhook/`
   - Include contact ID, message body, timestamp

### Workflow: "Notify Django on SMS Sent" (Optional)

1. **Trigger**: "Message Sent"  
2. **Action**: "Webhook" → POST to `https://yourdomain.com/ghl/webhook/`
   - Confirms delivery for tracking

---

## Step 9: Manual Send Button in Dispatching

Add a quick-send button to your leads view:

```python
# dispatching/views.py - Add endpoint

@login_required
@require_POST
def send_lead_sms(request):
    """Manually trigger SMS to a lead from dispatching UI."""
    from ghl_integration.tasks import sync_lead_to_ghl_and_send_sms
    from reservations.models import Lead
    
    if not request.user.is_superuser:
        return JsonResponse({"error": "Unauthorized"}, status=403)
    
    lead_id = request.POST.get('lead_id')
    
    try:
        lead = Lead.objects.get(id=lead_id)
        
        if lead.initial_sms_sent:
            return JsonResponse({
                "success": False, 
                "error": "SMS already sent to this lead"
            })
        
        if not lead.phone:
            return JsonResponse({
                "success": False,
                "error": "Lead has no phone number"
            })
        
        # Queue the task
        sync_lead_to_ghl_and_send_sms.delay(lead_id)
        
        return JsonResponse({
            "success": True,
            "message": f"SMS queued for {lead.get_full_name}"
        })
        
    except Lead.DoesNotExist:
        return JsonResponse({"success": False, "error": "Lead not found"}, status=404)
```

---

## Complete Flow Summary

### Automatic Flow (Hourly Batch):
1. Celery Beat triggers `batch_send_unsent_leads` every hour
2. Task finds all leads where `initial_sms_sent=False`
3. For each lead:
   - Create/update contact in GHL via API
   - Send SMS via GHL API
   - Update Django: `status="contacted"`, `initial_sms_sent=True`
4. Customer receives SMS

### Manual Flow (Admin Action):
1. Go to Lead admin
2. Select leads
3. Click "Send SMS to selected leads"
4. Same process as above

### When Customer Replies:
1. Customer replies to SMS
2. GHL receives reply, shows in Conversations inbox
3. GHL workflow fires webhook to Django
4. Django updates lead: `has_replied=True`, upgrades priority
5. NTFY sends you push notification "🔥 Lead Replied!"
6. You respond in GHL Conversations inbox (mobile or web)

---

## Environment Variables Needed

```bash
# .env
GHL_API_KEY=your_location_api_key_here
GHL_LOCATION_ID=your_location_id_here
GHL_WEBHOOK_SECRET=optional_signing_secret
```

To get these:
1. GHL → Settings → Business Profile → Copy Location ID
2. GHL → Settings → API Keys → Create Location API Key

---

## Testing Checklist

1. [ ] Create test lead in Django admin
2. [ ] Run `sync_lead_to_ghl_and_send_sms.delay(lead_id)` manually
3. [ ] Verify contact appears in GHL
4. [ ] Verify SMS received on test phone
5. [ ] Reply to SMS
6. [ ] Verify reply appears in GHL Conversations
7. [ ] Verify Django lead updated with `has_replied=True`
8. [ ] Verify NTFY notification received
9. [ ] Test batch send with multiple leads
10. [ ] Test admin action "Send SMS to selected"
