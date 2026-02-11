# GoHighLevel "Invalid JWT" Error - Troubleshooting Guide

## The Problem

You're getting: `{"statusCode":401,"message":"Invalid JWT"}`

This means GoHighLevel is rejecting your API key/token.

---

## Common Causes & Solutions

### 1. Wrong Token Type (MOST COMMON)

**Problem:** You might be using an OAuth token instead of a Private Integration Token.

**Solution:** You need a **Private Integration Token**, not an OAuth token.

**How to Get the Correct Token:**

1. Log into your GoHighLevel account
2. Go to **Settings** → **Integrations** → **Private Integrations**
3. Click **"Create Private Integration"** or use an existing one
4. Copy the **API Key** (this is your Private Integration Token)
5. Make sure it has permissions for:
   - ✅ Contacts (read/write)
   - ✅ Conversations/Messages (read/write)

**Token Format:**
- Private Integration Tokens are usually long strings (50+ characters)
- They don't expire (unless you revoke them)
- They start with various prefixes depending on GHL version

---

### 2. Token Doesn't Have Location Access

**Problem:** Your token might not have access to the Location ID you're using.

**Solution:**
1. In GHL, go to **Settings** → **Integrations** → **Private Integrations**
2. Edit your integration
3. Make sure the **Location** is selected (or "All Locations")
4. Verify the Location ID matches what you have in settings

**How to Find Your Location ID:**
1. In GHL dashboard, look at the URL: `https://app.gohighlevel.com/location/{LOCATION_ID}/...`
2. Or go to Settings → Locations → Your Location → The ID is in the URL or settings

---

### 3. Token Expired or Revoked

**Problem:** The token was revoked or expired.

**Solution:**
1. Generate a new Private Integration Token
2. Update your `GHL_API_KEY` environment variable
3. Restart your Django server

---

### 4. Wrong API Endpoint or Version

**Problem:** Using wrong base URL or API version.

**Current Implementation (should be correct):**
- Base URL: `https://services.leadconnectorhq.com`
- Version Header: `2021-07-28`

If you're using a different GHL account type or region, the endpoint might differ.

---

## Diagnostic Test

Run this in Django shell to get detailed diagnostics:

```python
from ghl_integration.services import GoHighLevelService

service = GoHighLevelService()
diagnostics = service.test_api_connection()

print("=== GHL API Diagnostics ===")
print(f"API Key Set: {diagnostics['api_key_set']}")
print(f"Location ID Set: {diagnostics['location_id_set']}")
print(f"API Key Length: {diagnostics['api_key_length']}")
print(f"API Key Preview: {diagnostics['api_key_prefix']}")
print(f"Connection Test: {diagnostics['connection_test']}")
if diagnostics['error']:
    print(f"❌ Error: {diagnostics['error']}")
    if 'error_details' in diagnostics:
        print(f"Error Details: {diagnostics['error_details']}")
else:
    print("✅ Connection successful!")
```

---

## Step-by-Step Fix

### Step 1: Verify Token Type

```python
# In Django shell
from django.conf import settings
import os

api_key = os.environ.get('GHL_API_KEY', '')
print(f"API Key Length: {len(api_key)}")
print(f"API Key Preview: {api_key[:20]}...{api_key[-10:] if len(api_key) > 30 else ''}")
```

**Expected:**
- Length: Usually 50-100+ characters
- Format: Long alphanumeric string

**If it's short (< 30 chars):** You might have an OAuth token instead.

---

### Step 2: Test Token Directly with curl

Replace `YOUR_TOKEN` and `YOUR_LOCATION_ID`:

```bash
curl --request GET \
  --url "https://services.leadconnectorhq.com/contacts/?locationId=YOUR_LOCATION_ID&limit=1" \
  --header 'Authorization: Bearer YOUR_TOKEN' \
  --header 'Content-Type: application/json' \
  --header 'Version: 2021-07-28'
```

**If this works:** The token is valid, issue is in Django code.
**If this fails:** The token itself is invalid.

---

### Step 3: Check Token Permissions

In GHL dashboard:
1. Go to **Settings** → **Integrations** → **Private Integrations**
2. Click on your integration
3. Check **Scopes/Permissions**:
   - Must have: `contacts.read`, `contacts.write`
   - For SMS: `conversations.read`, `conversations.write`

---

### Step 4: Verify Location ID

```python
# In Django shell
from django.conf import settings
import os

location_id = os.environ.get('GHL_LOCATION_ID', '')
print(f"Location ID: {location_id}")

# Check if it matches what's in your GHL dashboard URL
```

---

## Quick Test Script

Save this and run: `python manage.py shell < test_ghl_auth.py`

```python
import os
from django.conf import settings
from ghl_integration.services import GoHighLevelService

print("=" * 50)
print("GHL API Authentication Test")
print("=" * 50)

# Check environment variables
api_key = os.environ.get('GHL_API_KEY', '')
location_id = os.environ.get('GHL_LOCATION_ID', '')

print(f"\n1. Environment Variables:")
print(f"   GHL_API_KEY: {'✅ Set' if api_key else '❌ NOT SET'} (length: {len(api_key)})")
print(f"   GHL_LOCATION_ID: {'✅ Set' if location_id else '❌ NOT SET'}")

if not api_key or not location_id:
    print("\n❌ ERROR: Missing environment variables!")
    print("   Set GHL_API_KEY and GHL_LOCATION_ID in your .env file")
    exit()

# Initialize service
print(f"\n2. Initializing Service...")
service = GoHighLevelService()

# Run diagnostics
print(f"\n3. Running Connection Test...")
diagnostics = service.test_api_connection()

print(f"\n4. Results:")
print(f"   API Key Length: {diagnostics['api_key_length']}")
print(f"   API Key Preview: {diagnostics['api_key_prefix']}")
print(f"   Connection Test Status: {diagnostics['connection_test']}")

if diagnostics['error']:
    print(f"\n❌ FAILED: {diagnostics['error']}")
    if 'error_details' in diagnostics:
        print(f"   Details: {diagnostics['error_details']}")
    print(f"\n💡 TROUBLESHOOTING:")
    print(f"   1. Verify you're using a Private Integration Token (not OAuth)")
    print(f"   2. Check token has permissions for Contacts and Conversations")
    print(f"   3. Verify Location ID is correct")
    print(f"   4. Try generating a new token in GHL dashboard")
else:
    print(f"\n✅ SUCCESS! API connection is working!")
```

---

## Still Not Working?

1. **Double-check token in GHL dashboard:**
   - Settings → Integrations → Private Integrations
   - Make sure you copied the full token (no spaces, no line breaks)

2. **Try generating a new token:**
   - Delete the old one
   - Create a new Private Integration
   - Copy the new token
   - Update your `.env` file
   - Restart Django

3. **Check GHL account type:**
   - Some GHL accounts might use different endpoints
   - Contact GHL support if you're on a custom/enterprise plan

4. **Verify you're using the right GHL account:**
   - Make sure the Location ID matches the account you're logged into

---

## Need More Help?

Check the logs for:
- Exact headers being sent (look for "GHL API Request - Headers")
- Full error response from GHL
- API key preview (first/last few characters)

The diagnostic method will help identify the exact issue!
