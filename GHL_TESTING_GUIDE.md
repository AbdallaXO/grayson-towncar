# GoHighLevel Integration Testing Guide

## Prerequisites Checklist

Before testing, make sure:

1. **Environment Variables Set:**
   ```bash
   # In your .env file or environment:
   GHL_API_KEY=your_api_key_here
   GHL_LOCATION_ID=your_location_id_here
   ```

2. **Django Settings:**
   - Settings are configured in `business/settings.py`
   - App is in `INSTALLED_APPS`

3. **Database Migrations:**
   ```bash
   python manage.py migrate
   ```

4. **Celery (if testing tasks):**
   - Celery worker running: `celery -A business worker -l info`
   - Celery beat running (for scheduled tasks): `celery -A business beat -l info`

---

## Testing Methods

### Method 1: Test Service Directly (Django Shell) - RECOMMENDED FIRST

This is the fastest way to test if the API connection works:

```bash
python manage.py shell
```

Then in the shell:

```python
# Import required modules
from reservations.models import Lead
from ghl_integration.services import GoHighLevelService, get_sms_template

# Get or create a test lead with a phone number
lead = Lead.objects.filter(phone__isnull=False).exclude(phone="").first()

# If no lead exists, create one:
if not lead:
    lead = Lead.objects.create(
        first_name="Test",
        last_name="User",
        phone="4075551234",  # Use a real phone number for testing
        email="test@example.com",
        pickup_location="Orlando Airport",
        dropoff_location="Disney World",
        status="new"
    )

# Test 1: Initialize service (check credentials)
service = GoHighLevelService()
print(f"API Key set: {bool(service.api_key)}")
print(f"Location ID set: {bool(service.location_id)}")
print(f"Headers: {service.headers}")

# Test 2: Test phone formatting
formatted = service._format_phone(lead.phone)
print(f"Original: {lead.phone}, Formatted: {formatted}")

# Test 3: Create/update contact in GHL
print("\n--- Testing Contact Creation ---")
contact_id = service.create_or_update_contact(lead)
print(f"Contact ID: {contact_id}")

# Test 4: Check if lead was updated
lead.refresh_from_db()
print(f"Lead GHL Contact ID: {lead.ghl_contact_id}")
print(f"Lead synced at: {lead.ghl_synced_at}")

# Test 5: Test SMS template generation
message = get_sms_template(lead)
print(f"\nSMS Template:\n{message}")

# Test 6: Send SMS (only if contact was created successfully)
if contact_id:
    print("\n--- Testing SMS Send ---")
    sms_result = service.send_sms(contact_id, "Test message from Django")
    print(f"SMS sent: {sms_result}")
```

**What to check:**
- ✅ No "Invalid JWT" errors
- ✅ Contact ID is returned
- ✅ Lead's `ghl_contact_id` is updated
- ✅ Check Django logs for debug output

---

### Method 2: Test via Django Admin Actions

1. **Go to Django Admin:**
   - Navigate to `/admin/reservations/lead/`

2. **Select a lead** (or multiple leads) that:
   - Has a phone number
   - `initial_sms_sent = False`

3. **Use Admin Actions:**
   - **Option A:** "📱 Send SMS to selected leads" - Syncs AND sends SMS
   - **Option B:** "🔄 Sync to GHL without SMS" - Just syncs contact (no SMS)

4. **Check Results:**
   - Success message shows count of queued leads
   - Check lead detail page - `ghl_contact_id` should be populated
   - Check Celery logs if using tasks

---

### Method 3: Test Celery Tasks Directly

```bash
python manage.py shell
```

```python
from reservations.models import Lead
from ghl_integration.tasks import sync_lead_to_ghl_and_send_sms, sync_lead_to_ghl_without_sms

# Get a test lead
lead = Lead.objects.filter(phone__isnull=False).exclude(phone="").first()

# Test sync without SMS (safer for testing)
result = sync_lead_to_ghl_without_sms(lead.id)  # Call directly (synchronous)
print(result)

# Or queue it (asynchronous - requires Celery worker)
# sync_lead_to_ghl_without_sms.delay(lead.id)

# Test full sync with SMS
# result = sync_lead_to_ghl_and_send_sms(lead.id)  # Call directly
# print(result)
```

---

### Method 4: Test Webhook Endpoint

1. **Get your webhook URL:**
   ```
   https://yourdomain.com/ghl/webhook/
   ```

2. **Test with curl:**
   ```bash
   curl -X POST https://yourdomain.com/ghl/webhook/ \
     -H "Content-Type: application/json" \
     -d '{
       "type": "InboundMessage",
       "contactId": "test_contact_id_123",
       "body": "Test reply message"
     }'
   ```

3. **Or use a tool like Postman/Insomnia** to send POST requests

**Note:** The webhook will only work if you have a lead with `ghl_contact_id` matching the `contactId` in the webhook payload.

---

## Debugging & Logs

### Enable Debug Logging

In Django settings, make sure logging is configured:

```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'ghl_integration': {
            'handlers': ['console'],
            'level': 'DEBUG',  # Set to DEBUG to see all API requests
            'propagate': True,
        },
    },
}
```

### What to Look For in Logs

**Successful Request:**
```
GHL API Request - Headers: {'Authorization': 'Bearer ...', ...}
GHL API Request - POST https://services.leadconnectorhq.com/contacts/
GHL API Response - Status: 200
Created GHL contact abc123 for lead 1
```

**Error (Invalid JWT):**
```
GHL API Response - Status: 401
Error creating contact: 401 - Full response: {"message": "Invalid JWT", ...}
```

---

## Common Issues & Solutions

### Issue 1: "Invalid JWT" Error

**Possible Causes:**
- API key is incorrect or expired
- API key format is wrong (should be a Private Integration Token)
- API key doesn't have proper permissions

**Solutions:**
1. Verify API key in GHL dashboard
2. Make sure you're using a **Private Integration Token** (not OAuth token)
3. Check that the token has permissions for Contacts and Conversations

### Issue 2: "Location ID not found"

**Solutions:**
1. Verify `GHL_LOCATION_ID` is correct
2. Make sure the API key has access to that location

### Issue 3: Contact Created but No SMS Sent

**Check:**
- SMS endpoint might need different format
- Check if `send_sms` returns `True`
- Verify contact has valid phone number in GHL

### Issue 4: No Debug Logs Appearing

**Solutions:**
1. Set logging level to `DEBUG` (see above)
2. Check if logger is configured correctly
3. Run with `python manage.py runserver --verbosity 2`

---

## Quick Test Script

Save this as `test_ghl.py` and run: `python manage.py shell < test_ghl.py`

```python
from reservations.models import Lead
from ghl_integration.services import GoHighLevelService
import logging

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('ghl_integration')
logger.setLevel(logging.DEBUG)

# Get or create test lead
lead = Lead.objects.filter(phone__isnull=False).exclude(phone="").first()
if not lead:
    print("No lead found. Create one first in admin.")
    exit()

print(f"Testing with Lead #{lead.id}: {lead.get_full_name}")
print(f"Phone: {lead.phone}")

# Test service
service = GoHighLevelService()
print(f"\nService initialized:")
print(f"  API Key: {'Set' if service.api_key else 'NOT SET'}")
print(f"  Location ID: {'Set' if service.location_id else 'NOT SET'}")

# Test contact creation
print(f"\nCreating/updating contact...")
contact_id = service.create_or_update_contact(lead)

if contact_id:
    print(f"✅ SUCCESS! Contact ID: {contact_id}")
    lead.refresh_from_db()
    print(f"   Lead updated: ghl_contact_id = {lead.ghl_contact_id}")
else:
    print("❌ FAILED! Check logs above for error details.")
```

---

## Next Steps After Testing

Once basic functionality works:

1. **Test with real phone numbers** (your own)
2. **Set up webhook** in GHL dashboard pointing to your webhook URL
3. **Test admin actions** with multiple leads
4. **Monitor Celery tasks** if using async processing
5. **Check GHL dashboard** to verify contacts are created

---

## Need Help?

Check logs for:
- Exact API request headers
- Full error response from GHL
- Contact data being sent
- Response status codes

The debug logging will show you exactly what's being sent to GHL, which helps diagnose any issues.
