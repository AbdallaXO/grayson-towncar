# Meta Custom Conversion Setup for Purchase Tracking

## Goal
Create a Custom Conversion that tracks Purchase events with conversion values so you can:
- See exact conversion values in Meta Ads Manager
- Optimize ads for purchase value (not just number of purchases)
- Track ROI accurately

## Step-by-Step Instructions

### Step 1: Create Custom Conversion

1. **Go to Meta Events Manager**
   - Visit: https://eventsmanager.facebook.com/events_manager2
   - Click on "Grayson Towncar Data" (your dataset)

2. **Navigate to Custom Conversions**
   - In the left sidebar, click **"Custom Conversions"**
   - Click the **"+ Create Custom Conversion"** button (top right)

3. **Configure the Conversion**

   **Name:**
   - Enter: `Website Purchase - All Purchases`
   - (Or any name you prefer)

   **Description (optional):**
   - "Tracks all purchase events with conversion values"

   **Event:**
   - Select: **"Purchase"** from the dropdown
   - This matches the event name we're sending from your code

   **URL Rules (optional):**
   - You can leave this empty to track ALL purchases
   - OR add a rule like: `URL contains "payment/success"` to only track from your success page

   **Value Settings:**
   - ✅ **Enable "Track conversion value"** (IMPORTANT!)
   - This will show you the dollar amounts

   **Conversion Window:**
   - Default: 7 days (good for most cases)
   - You can change to 1 day, 7 days, or 28 days

4. **Click "Create"**

### Step 2: Verify It's Working

1. **Wait 24-48 hours** for data to populate
2. **Check Custom Conversions:**
   - Go to Custom Conversions tab
   - Click on your new conversion
   - You should see:
     - Number of conversions
     - Total conversion value
     - Average conversion value

3. **View in Ads Manager:**
   - Go to Meta Ads Manager
   - When creating/editing campaigns, you can select this Custom Conversion
   - You'll see conversion value metrics

### Step 3: Use in Ad Campaigns

When creating ad campaigns:

1. **Campaign Objective:**
   - Choose "Conversions" or "Sales"

2. **Optimization Event:**
   - Select your Custom Conversion: "Website Purchase - All Purchases"
   - Meta will optimize for purchases with highest value

3. **Bid Strategy:**
   - Choose "Lowest cost" or "Cost cap"
   - Meta will try to get you purchases at the best value

4. **Conversion Tracking:**
   - In the ad set, under "Optimization & Delivery"
   - Select your Custom Conversion
   - Enable "Value optimization" if available

## What You'll See

### In Custom Conversions Dashboard:
- **Conversions:** Number of purchases
- **Conversion Value:** Total dollar amount
- **Average Value:** Average purchase amount
- **Cost per Conversion:** Ad spend / conversions
- **ROAS:** Return on ad spend

### In Ads Manager:
- **Purchase Value:** Total revenue from ads
- **Cost per Purchase:** How much you pay per purchase
- **ROAS:** Revenue / Ad Spend ratio
- **Value per Purchase:** Average purchase value

## Advanced: Value-Based Optimization

If you want Meta to optimize for HIGH-VALUE purchases:

1. **Create Value Rules:**
   - In Custom Conversion settings
   - Add rule: `value >= $X` (e.g., $100)
   - This creates a conversion for high-value purchases only

2. **Use in Campaigns:**
   - Optimize for the high-value conversion
   - Meta will prioritize users likely to make larger purchases

## Troubleshooting

**Not seeing values?**
- Make sure "Track conversion value" is enabled
- Verify Purchase events include `value` in custom_data (✅ already configured in code)
- Wait 24-48 hours for data to populate

**Values seem wrong?**
- Check that `reservation.total_price` is correct in your database
- Verify events are sending: Events Manager → Test Events tab

**Not enough data?**
- Custom Conversions need at least 1 conversion in 7 days to show data
- Make sure you're sending Purchase events (✅ already working)
