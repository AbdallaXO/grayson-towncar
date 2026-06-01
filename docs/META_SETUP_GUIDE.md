# Meta Conversions API Setup Guide

## Current Status
- ✅ Pixel ID: `1261740178962298` (confirmed correct)
- ⚠️ Only PageView events showing (Purchase events need CAPI token)

## Step 1: Get Your Conversions API Access Token

Since you have a **Conversions API Gateway** set up, you need to get the access token from the Gateway settings:

### Option A: From Gateway Settings (Recommended)

1. **In the Conversions API Gateway page you're viewing:**
   - Look for a section called **"Access Token"** or **"Server Access Token"**
   - It might be in a tab like **"Settings"**, **"Configuration"**, or **"Authentication"**
   - Click on the Gateway dropdown → Look for **"Settings"** or **"Manage"** option

2. **Alternative: Go to Dataset Settings**
   - Go back to **"Grayson Towncar Data"** dataset
   - Click the **"Settings"** tab
   - Look for **"Conversions API"** section
   - You should see **"Server Access Token"** or **"Generate Access Token"** button
   - Click it and copy the token (starts with `EAA...`)

### Option B: If You Don't See Token in Gateway

1. **Go to Meta Events Manager**
   - Visit: https://business.facebook.com/events_manager2
   - Click on **"Grayson Towncar Data"** (ID: 1261740178962298)

2. **Navigate to Settings**
   - Click the **"Settings"** tab at the top
   - Scroll down to **"Conversions API"** section
   - Click **"Set up manually"** or **"Generate Access Token"**
   - Copy the token (it will look like: `EAAxxxxxxxxxxxxxxxxxxxxx`)

**Note:** The Gateway you see (`mpc-prod-16-s6uit34pua-uk.a.run.app`) is Meta's managed service, but you still need the access token to authenticate your server-side API calls.

## Step 2: Set Environment Variables

Add these to your Railway environment variables (or `.env` file for local):

```bash
FB_PIXEL_ID=1261740178962298
FB_CAPI_ACCESS_TOKEN=your_token_here
```

**Important:** 
- Replace `your_token_here` with the actual token you copied
- No quotes needed in Railway (it handles that automatically)
- In `.env` file, use: `FB_CAPI_ACCESS_TOKEN="your_token_here"`

## Step 3: Verify Setup

After setting the environment variables:

1. **Restart your Django server** (Railway will auto-restart)
2. **Test a purchase** - Complete a test reservation and payment
3. **Check Events Manager** - Within 30 minutes, you should see:
   - ✅ Purchase events in the Events Manager
   - ✅ Both "Meta Pixel" and "Conversions API" as integration sources

## Step 4: Test Events (Optional)

You can test events directly in Events Manager:

1. Go to **"Test events"** tab in Events Manager
2. Enter your website URL: `www.graysontowncar.com`
3. Complete a test purchase
4. You should see Purchase events appear in real-time

## Troubleshooting

### No Purchase Events Showing?

1. **Check server logs** for errors:
   ```bash
   # Look for these log messages:
   # ✅ "Successfully sent Purchase event to Meta CAPI"
   # ❌ "Error sending Purchase event to Meta CAPI"
   # ❌ "Missing required environment variables"
   ```

2. **Verify environment variables are set:**
   - In Railway: Settings → Variables
   - Make sure `FB_PIXEL_ID` and `FB_CAPI_ACCESS_TOKEN` are both set

3. **Check token format:**
   - Should start with `EAA` or similar
   - No extra spaces or line breaks
   - Full token copied (can be 200+ characters)

4. **Verify Pixel ID matches:**
   - `FB_PIXEL_ID` should be exactly: `1261740178962298`
   - No quotes, no spaces

### Events Not Matching / Double-Counting?

- Purchase events use `reservation.total_price` (same as Google Analytics)
- Both Pixel (client-side) and CAPI (server-side) send events
- Meta deduplicates them using a **stable** `event_id` shared by the browser
  pixel and every server-side emitter for the same conversion:
  - **Purchase** → `event_id = <Stripe payment-intent id>` (success page,
    webhook, and dispatcher charge all use this; no timestamp)
  - **Lead** → `event_id = "quote_<quote.id>"` (returned by the quote
    endpoint and passed to `fbq('track','Lead', {}, {eventID})`)
- ⚠️ Never append a timestamp to `event_id` — different timestamps make the
  pixel and CAPI copies look like separate events and Meta will count both.
- In Events Manager, a correctly-deduped event shows **"Processed"** with the
  count from one source and a **"Deduplicated"** number from the other.

## What Events Are Being Sent?

1. **Lead** - When someone submits a quote form
2. **InitiateCheckout** - When someone creates a reservation
3. **Purchase** - When payment is successful (via webhook + success page)

All events include:
- ✅ Hashed customer data (email, phone, name, zipcode)
- ✅ Value and currency
- ✅ Transaction ID
- ✅ Event ID for deduplication
- ✅ IP address and User-Agent (when available)
