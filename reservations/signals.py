import logging
import time
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Reservation
from users.emails import send_reservation_confirmation
from django.db import transaction

logger = logging.getLogger(__name__)  # Get a logger instance

# Track which fields are relevant for HubSpot updates
HUBSPOT_RELEVANT_FIELDS = {
    'status', 'total_price', 'special_requests', 
    # Add other fields that should trigger a HubSpot update
}

@receiver(post_save, sender=Reservation)
def reservation_saved(sender, instance, created, **kwargs):
    """Sync reservation status and dates to HubSpot when saved with smart update detection"""
    # Skip if this is a new reservation (will be handled by create_reservation)
    if created:
        from users.emails import send_internal_confirmation
        
        # Run in a background task or thread
        from django.core.signals import request_finished
        from threading import Thread
        
        def background_tasks():
            # Create a local logger inside the function
            local_logger = logging.getLogger(__name__)
            
            # Send internal confirmation email
            try:
                # send_internal_confirmation(instance)
                local_logger.info(f"Internal confirmation sent for reservation #{instance.id}")
            except Exception as e:
                local_logger.error(f"Error sending internal confirmation for reservation #{instance.id}: {e}")
                
            # Add adaptive retry logic instead of fixed delay
            max_attempts = 3
            attempt = 0
            success = False
            
            while attempt < max_attempts and not success:
                attempt += 1
                local_logger.info(f"Attempting HubSpot sync for reservation #{instance.id} (attempt {attempt}/{max_attempts})")
                
                # Run HubSpot sync for new reservations
                try:
                    from reservations.hubspot_service import sync_reservation_to_hubspot
                    
                    # Refresh the reservation instance to get the latest data including legs
                    with transaction.atomic():
                        refreshed_instance = Reservation.objects.select_related(
                            'customer', 'vehicle'
                        ).prefetch_related(
                            'legs', 'payments'
                        ).get(id=instance.id)
                        
                    # Check if legs exist before syncing
                    if refreshed_instance.legs.exists():
                        hubspot_result = sync_reservation_to_hubspot(refreshed_instance)
                        if hubspot_result["success"]:
                            local_logger.info(f"Reservation #{refreshed_instance.id} synced to HubSpot: {hubspot_result}")
                            success = True
                            break
                        else:
                            local_logger.warning(f"Failed to sync reservation #{refreshed_instance.id} to HubSpot: {hubspot_result}")
                    else:
                        local_logger.warning(f"Legs not yet found for reservation #{refreshed_instance.id} - waiting before retry")
                        # Exponential backoff: 2s, 4s, 8s
                        wait_time = 2 ** attempt
                        time.sleep(wait_time)
                except Exception as e:
                    local_logger.error(f"Error syncing reservation to HubSpot: {e}")
                    # Exponential backoff on exception too
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
            
            if not success:
                local_logger.error(f"Failed to sync reservation #{instance.id} to HubSpot after {max_attempts} attempts")
        
        # Start a background thread to handle these tasks
        thread = Thread(target=background_tasks)
        thread.daemon = True  # Thread will exit when main thread exits
        thread.start()
        
        return
    
    # For existing reservations, check if we need to update HubSpot
    update_needed = False
    
    # Check if any relevant fields have changed
    if hasattr(instance, '_changed_fields'):
        update_needed = any(field in HUBSPOT_RELEVANT_FIELDS for field in instance._changed_fields)
    else:
        # If we don't have tracked fields, have to assume changes might be relevant
        update_needed = True
    
    # Optimize: Skip HubSpot update if no relevant changes
    if not update_needed:
        logger.info(f"Skipping HubSpot update for reservation #{instance.id} - no relevant changes")
        return
    
    # Import here to avoid circular imports
    from reservations.hubspot_service import find_deal_by_reservation_id, sync_reservation_to_hubspot
    
    # For existing reservations without a deal, do a full sync
    if not find_deal_by_reservation_id(instance.id):
        logger.info(f"No existing deal found for reservation #{instance.id} - doing full sync")
        # Use select_related and prefetch_related for efficiency
        refreshed_instance = Reservation.objects.select_related(
            'customer', 'vehicle'
        ).prefetch_related(
            'legs', 'payments'
        ).get(id=instance.id)
        sync_reservation_to_hubspot(refreshed_instance)
        return
    
    # For existing reservations with a deal, update status and dates
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
           # Check for pickup date/time changes
            legs = list(instance.legs.order_by("pickup_date", "pickup_time"))
            if legs:
                first_leg = legs[0]
                
                # Update closedate using pickup date/time if available
                if hasattr(first_leg, "pickup_date") and hasattr(first_leg, "pickup_time"):
                    try:
                        dt = datetime.combine(first_leg.pickup_date, first_leg.pickup_time).replace(
                            tzinfo=timezone.utc
                        )
                        close_ms = int(dt.timestamp() * 1000)
                        properties["closedate"] = close_ms
                    except Exception as e:
                        # Just log the error and continue
                        logger.error(f"Error formatting date for HubSpot: {e}")
            
            # Check for payment changes - get fresh payment data
            # Only if we've updated payment-related fields
            if hasattr(instance, '_changed_fields') and 'total_price' in instance._changed_fields:
                # Reload the instance with prefetch_related for payments
                refreshed = Reservation.objects.prefetch_related('payments').get(id=instance.id)
                from reservations.hubspot_service import get_reservation_payment_info
                payment_info = get_reservation_payment_info(refreshed)
                
                # Add payment information to properties
                properties["payment_status"] = payment_info["status"]
                properties["payment_amount"] = payment_info["amount"]
                properties["payment_method"] = payment_info["method"]
                properties["amount"] = payment_info["amount"]  # Update deal amount too
            
            # Update deal in HubSpot
            client.crm.deals.basic_api.update(
                deal_id=deal_id,
                simple_public_object_input=DealInput(properties=properties)
            )
            
            # Invalidate cache for this deal
            from reservations.hubspot_service import invalidate_deal_cache
            invalidate_deal_cache(instance.id)
            
            logger.info(f"Updated HubSpot deal {deal_id} for reservation #{instance.id}")
    except Exception as e:
        # Log error but don't raise - we don't want to break the save operation
        # Log error but don't raise - we don't want to break the save operation
        logger.error(f"Error updating HubSpot for reservation #{instance.id}: {e}")

