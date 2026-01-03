"""
Full GHL Integration Test Script

Run with: python manage.py shell < test_ghl_full.py
Or copy/paste into Django shell
"""

from reservations.models import Lead
from ghl_integration.services import GoHighLevelService, get_sms_template

print("=" * 60)
print("GHL Integration Full Test")
print("=" * 60)

# Step 1: Get or create a test lead
print("\n1. Getting test lead...")
lead = Lead.objects.filter(phone__isnull=False).exclude(phone="").first()

if not lead:
    print("   No lead found. Creating test lead...")
    lead = Lead.objects.create(
        first_name="Test",
        last_name="User",
        phone="4075551234",  # Use your own phone number for real testing
        email="test@example.com",
        pickup_location="Orlando Airport (MCO)",
        dropoff_location="Disney World",
        status="new"
    )
    print(f"   ✅ Created test lead #{lead.id}")
else:
    print(f"   ✅ Using existing lead #{lead.id}: {lead.get_full_name}")

print(f"   Phone: {lead.phone}")
print(f"   Current GHL Contact ID: {lead.ghl_contact_id or 'None'}")

# Step 2: Initialize service
print("\n2. Initializing GHL Service...")
service = GoHighLevelService()

# Step 3: Test contact creation/update
print("\n3. Testing contact creation/update in GHL...")
contact_id = service.create_or_update_contact(lead)

if contact_id:
    print(f"   ✅ SUCCESS! Contact ID: {contact_id}")
    
    # Refresh lead to see if it was updated
    lead.refresh_from_db()
    print(f"   Lead updated:")
    print(f"     - ghl_contact_id: {lead.ghl_contact_id}")
    print(f"     - ghl_synced_at: {lead.ghl_synced_at}")
else:
    print(f"   ❌ FAILED to create/update contact")
    print(f"   Check logs above for error details")
    exit()

# Step 4: Test SMS template generation
print("\n4. Testing SMS template generation...")
message = get_sms_template(lead)
print(f"   SMS Template:")
print(f"   {message}")

# Step 5: Test SMS sending (optional - comment out if you don't want to send real SMS)
print("\n5. Testing SMS send...")
print("   ⚠️  This will send a real SMS! Uncomment the code below to test.")
print("   Or use: sms_result = service.send_sms(contact_id, 'Test message')")

# Uncomment to actually send SMS:
# sms_result = service.send_sms(contact_id, "Test message from Django GHL integration")
# if sms_result:
#     print(f"   ✅ SMS sent successfully!")
# else:
#     print(f"   ❌ Failed to send SMS")

# Step 6: Summary
print("\n" + "=" * 60)
print("Test Summary")
print("=" * 60)
print(f"✅ API Connection: Working")
print(f"✅ Contact Creation: {'Working' if contact_id else 'Failed'}")
print(f"✅ Lead Update: {'Working' if lead.ghl_contact_id else 'Failed'}")
print(f"✅ SMS Template: Generated")
print(f"\nNext Steps:")
print(f"1. Check your GHL dashboard - contact should be there")
print(f"2. Test admin actions in Django admin")
print(f"3. Test Celery tasks if using async processing")
print(f"4. Set up webhook in GHL dashboard")
