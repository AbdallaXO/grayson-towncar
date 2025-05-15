import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Reservation
from users.emails import send_reservation_confirmation

logger = logging.getLogger(__name__)  # Get a logger instance


@receiver(post_save, sender=Reservation)
def reservation_saved(sender, instance, created, **kwargs):
    """Sync reservation status and dates to HubSpot when saved"""
    # Skip if this is a new reservation (will be handled by create_reservation)
    if created:
        return
    
    # Import here to avoid circular imports
    from reservations.hubspot_service import find_deal_by_reservation_id, sync_reservation_to_hubspot
    
    # For new reservations, do a full sync
    if not find_deal_by_reservation_id(instance.id):
        sync_reservation_to_hubspot(instance)
        return
    
    # For existing reservations, update status and dates
    from hubspot import HubSpot
    from hubspot.crm.deals import SimplePublicObjectInput as DealInput
    import os
    from datetime import datetime, timezone
    
    HUBSPOT_TOKEN = os.environ.get('HUBSPOT_TOKEN')
    if not HUBSPOT_TOKEN:
        return
    
    try:
        client = HubSpot(access_token=HUBSPOT_TOKEN)
        deal_id = find_deal_by_reservation_id(instance.id)
        
        if deal_id:
            # Map reservation status to HubSpot-friendly values
            status_map = {
                "pending": "Pending",
                "confirmed": "Confirmed",
                "in_progress": "In Progress",
                "completed": "Completed",
                "cancelled": "Cancelled"
            }
            hubspot_status = status_map.get(instance.status, instance.status.replace("_", " ").title())
            
            # Start with basic properties
            properties = {
                "reservation_status": hubspot_status
            }
            
            # Check for pickup date/time changes
            legs = list(instance.legs.order_by("pickup_date", "pickup_time"))
            if legs:
                first_leg = legs[0]
                
                # Update pickup date/time if available
                if hasattr(first_leg, "pickup_date") and hasattr(first_leg, "pickup_time"):
                    # Format as string for HubSpot
                    pickup_datetime_str = f"{first_leg.pickup_date} {first_leg.pickup_time}"
                    properties["pickup_date_time"] = pickup_datetime_str
                    
                    # For closedate, we need milliseconds timestamp
                    try:
                        dt = datetime.combine(first_leg.pickup_date, first_leg.pickup_time).replace(
                            tzinfo=timezone.utc
                        )
                        close_ms = int(dt.timestamp() * 1000)
                        properties["closedate"] = close_ms
                    except Exception as e:
                        # Just log the error and continue
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.error(f"Error formatting date for HubSpot: {e}")
                
                # Update pickup location if changed
                if hasattr(first_leg, "pickup_location"):
                    properties["pickup_location"] = first_leg.pickup_location
                
                # Update dropoff location if changed
                if hasattr(first_leg, "dropoff_location"):
                    properties["dropoff_location"] = first_leg.dropoff_location
            
            # Update deal in HubSpot
            client.crm.deals.basic_api.update(
                deal_id=deal_id,
                simple_public_object_input=DealInput(properties=properties)
            )
    except Exception as e:
        # Log error but don't raise - we don't want to break the save operation
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error updating HubSpot for reservation #{instance.id}: {e}")