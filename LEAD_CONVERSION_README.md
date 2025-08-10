# Automatic Lead Conversion System

This system automatically converts leads to "converted" status when they make reservations, making it easy to track your conversion rates and manage your sales pipeline.

## How It Works

### 1. **Automatic Conversion (Real-time)**

- When a new reservation is created, the system automatically checks for matching leads
- **Matching criteria**: Email address OR phone number (case-insensitive)
- **Auto-updates**: Lead status → "converted", sets `converted=True`, records `converted_at` timestamp
- **Notes**: Adds automatic conversion note with reservation ID and timestamp

### 2. **Admin Actions**

- **"Check for Auto-Conversion"**: Bulk action to find and convert leads that should have been converted (useful for existing data)
- **"Mark as Converted"**: Manual conversion for leads that don't have matching reservations

### 3. **Visual Indicators**

- **Status badges**: Bright, colorful status indicators with background colors
- **Conversion info**: Shows conversion date under "converted" status
- **Admin dashboard**: Conversion rate statistics and analytics

## Setup

The system is already configured and ready to use:

1. **Signals are loaded** via `reservations/apps.py`
2. **Admin actions** are available in the Lead admin interface
3. **No database changes** required

## Usage

### For New Leads (Automatic)

1. Create a lead in the admin
2. When the customer makes a reservation, the lead automatically converts
3. The lead status changes to "converted" with timestamp

### For Existing Leads (Manual)

1. Select leads in the admin
2. Use "Check for Auto-Conversion" action to find matching reservations
3. System will convert leads that have matching reservations

### Testing the System

```bash
# Create test data
python manage.py test_auto_conversion --create-test-data

# Check which leads should be converted
python manage.py test_auto_conversion --check-conversions
```

## Benefits

✅ **No manual work** - leads convert automatically  
✅ **Accurate tracking** - real-time conversion data  
✅ **Easy management** - bulk actions for existing data  
✅ **Visual feedback** - colorful status indicators  
✅ **Audit trail** - conversion notes and timestamps

## Technical Details

- **Signal**: `post_save` on Reservation model
- **Matching**: Email (primary) → Phone (fallback)
- **Status update**: Only converts leads not already converted
- **Performance**: Efficient database queries with proper indexing
- **Logging**: Console output for debugging

## Troubleshooting

**Leads not converting?**

1. Check if email/phone matches exactly between lead and customer
2. Verify the reservation was created (not just saved)
3. Check Django console for conversion logs

**Need to convert existing leads?**

1. Use "Check for Auto-Conversion" admin action
2. System will find leads with matching reservations
3. Convert them automatically

**Want to test?**

1. Use the management command to create test data
2. Watch leads convert in real-time
3. Check admin interface for conversion status
