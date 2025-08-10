import logging
import time
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Reservation
from users.emails import send_reservation_confirmation
from django.db import transaction
from decimal import Decimal
from django.utils import timezone
from .models import Reservation, Lead

logger = logging.getLogger(__name__)  # Get a logger instance

# Track which fields are relevant for HubSpot updates
HUBSPOT_RELEVANT_FIELDS = {
    "status",
    "total_price",
    "special_requests",
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
                send_internal_confirmation(instance)
                local_logger.info(
                    f"Internal confirmation sent for reservation #{instance.id}"
                )
            except Exception as e:
                local_logger.error(
                    f"Error sending internal confirmation for reservation #{instance.id}: {e}"
                )

            # Add adaptive retry logic instead of fixed delay
            max_attempts = 3
            attempt = 0
            success = False

            while attempt < max_attempts and not success:
                attempt += 1
                local_logger.info(
                    f"Attempting HubSpot sync for reservation #{instance.id} (attempt {attempt}/{max_attempts})"
                )

                # Run HubSpot sync for new reservations
                try:
                    from reservations.hubspot_service import sync_reservation_to_hubspot

                    # Refresh the reservation instance to get the latest data including legs
                    with transaction.atomic():
                        refreshed_instance = (
                            Reservation.objects.select_related("customer", "vehicle")
                            .prefetch_related("legs", "payments")
                            .get(id=instance.id)
                        )

                    # Check if legs exist before syncing
                    if refreshed_instance.legs.exists():
                        hubspot_result = sync_reservation_to_hubspot(refreshed_instance)
                        if hubspot_result["success"]:
                            local_logger.info(
                                f"Reservation #{refreshed_instance.id} synced to HubSpot: {hubspot_result}"
                            )
                            success = True
                            break
                        else:
                            local_logger.warning(
                                f"Failed to sync reservation #{refreshed_instance.id} to HubSpot: {hubspot_result}"
                            )
                    else:
                        local_logger.warning(
                            f"Legs not yet found for reservation #{refreshed_instance.id} - waiting before retry"
                        )
                        # Exponential backoff: 2s, 4s, 8s
                        wait_time = 2**attempt
                        time.sleep(wait_time)
                except Exception as e:
                    local_logger.error(f"Error syncing reservation to HubSpot: {e}")
                    # Exponential backoff on exception too
                    wait_time = 2**attempt
                    time.sleep(wait_time)

            if not success:
                local_logger.error(
                    f"Failed to sync reservation #{instance.id} to HubSpot after {max_attempts} attempts"
                )

        # Start a background thread to handle these tasks
        thread = Thread(target=background_tasks)
        thread.daemon = True  # Thread will exit when main thread exits
        thread.start()

        return

    # For existing reservations, check if we need to update HubSpot
    update_needed = False

    # Check if any relevant fields have changed
    if hasattr(instance, "_changed_fields"):
        update_needed = any(
            field in HUBSPOT_RELEVANT_FIELDS for field in instance._changed_fields
        )
    else:
        # If we don't have tracked fields, have to assume changes might be relevant
        update_needed = True

    # Optimize: Skip HubSpot update if no relevant changes
    if not update_needed:
        logger.info(
            f"Skipping HubSpot update for reservation #{instance.id} - no relevant changes"
        )
        return

    # Import here to avoid circular imports
    from reservations.hubspot_service import (
        find_deal_by_reservation_id,
        sync_reservation_to_hubspot,
    )

    # For existing reservations without a deal, do a full sync
    if not find_deal_by_reservation_id(instance.id):
        logger.info(
            f"No existing deal found for reservation #{instance.id} - doing full sync"
        )
        # Use select_related and prefetch_related for efficiency
        refreshed_instance = (
            Reservation.objects.select_related("customer", "vehicle")
            .prefetch_related("legs", "payments")
            .get(id=instance.id)
        )
        sync_reservation_to_hubspot(refreshed_instance)
        return

    # For existing reservations with a deal, update status and dates
    from hubspot import HubSpot
    from hubspot.crm.deals import SimplePublicObjectInput as DealInput
    import os
    from datetime import datetime, timezone

    HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN")
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
                "cancelled": "Cancelled",
            }
            hubspot_status = status_map.get(
                instance.status, instance.status.replace("_", " ").title()
            )

            # Start with basic properties
            properties = {"reservation_status": hubspot_status}

            # Check for pickup date/time changes
            legs = list(instance.legs.order_by("pickup_date", "pickup_time"))
            # Check for pickup date/time changes
            legs = list(instance.legs.order_by("pickup_date", "pickup_time"))
            if legs:
                first_leg = legs[0]

                # Update closedate using pickup date/time if available
                if hasattr(first_leg, "pickup_date") and hasattr(
                    first_leg, "pickup_time"
                ):
                    try:
                        dt = datetime.combine(
                            first_leg.pickup_date, first_leg.pickup_time
                        ).replace(tzinfo=timezone.utc)
                        close_ms = int(dt.timestamp() * 1000)
                        properties["closedate"] = close_ms
                    except Exception as e:
                        # Just log the error and continue
                        logger.error(f"Error formatting date for HubSpot: {e}")

            # Check for payment changes - get fresh payment data
            # Only if we've updated payment-related fields
            if (
                hasattr(instance, "_changed_fields")
                and "total_price" in instance._changed_fields
            ):
                # Reload the instance with prefetch_related for payments
                refreshed = Reservation.objects.prefetch_related("payments").get(
                    id=instance.id
                )
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
                simple_public_object_input=DealInput(properties=properties),
            )

            # Invalidate cache for this deal
            from reservations.hubspot_service import invalidate_deal_cache

            invalidate_deal_cache(instance.id)

            logger.info(
                f"Updated HubSpot deal {deal_id} for reservation #{instance.id}"
            )
    except Exception as e:
        # Log error but don't raise - we don't want to break the save operation
        # Log error but don't raise - we don't want to break the save operation
        logger.error(f"Error updating HubSpot for reservation #{instance.id}: {e}")


# In reservations/models.py or a new file reservations/signals.py
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from reservations.models import Reservation
from django.db.models import Sum


@receiver(post_save, sender=Reservation)
def update_agent_commission_data(sender, instance, created, **kwargs):
    """
    Update the travel agent's commission data when a reservation is saved.
    Handles commission-related fields: pending and unpaid
    """
    if instance.travel_agent:
        agent = instance.travel_agent
        update_fields = []

        # Track if status changed
        status_changed = False
        old_status = None
        if (
            not created
            and hasattr(instance, "_changed_fields")
            and "status" in instance._changed_fields
        ):
            try:
                old_instance = Reservation.objects.get(pk=instance.pk)
                old_status = old_instance.status
                status_changed = True
            except Reservation.DoesNotExist:
                pass

        # Handle status changes specifically
        if status_changed:
            logger.info(
                f"Reservation #{instance.id} status changed from {old_status} to {instance.status}"
            )

            # If changed from confirmed to something else, recalculate pending
            if old_status == "confirmed":
                recalculate_pending = True

            # If changed from completed to something else, recalculate unpaid
            if old_status == "completed" and not instance.commission_paid:
                recalculate_unpaid = True

            # If changed to cancelled, ensure commission is properly handled
            if instance.status == "cancelled":
                logger.info(
                    f"Reservation #{instance.id} cancelled - adjusting commission data"
                )
                # No commissions for cancelled reservations
                instance.commission_amount = Decimal("0.00")
                # This will save the instance again, but that's OK
                instance.save(update_fields=["commission_amount"])

        # Calculate pending commissions (confirmed but not completed)
        pending_total = Reservation.objects.filter(
            travel_agent=agent, status="confirmed"
        ).aggregate(total=Sum("commission_amount"))["total"] or Decimal("0")

        # Update pending commissions if changed
        if agent.pending_commissions != pending_total:
            logger.info(
                f"Updating agent {agent} pending commissions from ${agent.pending_commissions} to ${pending_total}"
            )
            agent.pending_commissions = pending_total
            update_fields.append("pending_commissions")

        # Calculate unpaid commissions (completed but not paid)
        unpaid_total = Reservation.objects.filter(
            travel_agent=agent, commission_paid=False, status="completed"
        ).aggregate(total=Sum("commission_amount"))["total"] or Decimal("0")

        # Update unpaid commissions if changed
        if agent.unpaid_commissions != unpaid_total:
            logger.info(
                f"Updating agent {agent} unpaid commissions from ${agent.unpaid_commissions} to ${unpaid_total}"
            )
            agent.unpaid_commissions = unpaid_total
            update_fields.append("unpaid_commissions")

        # Save agent if any fields were updated
        if update_fields:
            agent.save(update_fields=update_fields)


@receiver(pre_delete, sender=Reservation)
def update_agent_commission_on_delete(sender, instance, **kwargs):
    """
    Update the travel agent's commission data when a reservation is deleted.
    """
    if instance.travel_agent:
        agent = instance.travel_agent
        update_fields = []

        logger.info(
            f"Deleting reservation #{instance.id} with status {instance.status} - adjusting commission data"
        )

        # For confirmed reservations, adjust pending commissions
        if instance.status == "confirmed":
            # Either recalculate or subtract directly
            new_pending = max(
                Decimal("0"), agent.pending_commissions - instance.commission_amount
            )

            logger.info(
                f"Updating agent {agent} pending commissions from ${agent.pending_commissions} to ${new_pending}"
            )
            agent.pending_commissions = new_pending
            update_fields.append("pending_commissions")

        # For completed & unpaid reservations, adjust unpaid commissions
        if instance.status == "completed" and not instance.commission_paid:
            # Either recalculate or subtract directly
            new_unpaid = max(
                Decimal("0"), agent.unpaid_commissions - instance.commission_amount
            )

            logger.info(
                f"Updating agent {agent} unpaid commissions from ${agent.unpaid_commissions} to ${new_unpaid}"
            )
            agent.unpaid_commissions = new_unpaid
            update_fields.append("unpaid_commissions")

        # Save agent if any fields were updated
        if update_fields:
            agent.save(update_fields=update_fields)


@receiver(post_save, sender=Reservation)
def auto_convert_lead_on_reservation(sender, instance, created, **kwargs):
    """
    Automatically convert leads to 'converted' status when a reservation is created.
    This matches leads by email or phone number to find the corresponding lead.
    """
    if created:  # Only run when a new reservation is created
        customer = instance.customer
        
        # Try to find a matching lead by email first, then by phone
        matching_lead = None
        
        if customer.email:
            matching_lead = Lead.objects.filter(
                email__iexact=customer.email,
                status__in=['new', 'contacted', 'interested', 'future_contact']
            ).first()
        
        # If no match by email, try by phone
        if not matching_lead and customer.phone_number:
            matching_lead = Lead.objects.filter(
                phone__iexact=customer.phone_number,
                status__in=['new', 'contacted', 'interested', 'future_contact']
            ).first()
        
        # If we found a matching lead, convert it
        if matching_lead:
            matching_lead.status = 'converted'
            matching_lead.converted = True
            matching_lead.converted_at = timezone.now()
            
            # Add a note about the conversion
            conversion_note = f"Auto-converted on {timezone.now().strftime('%Y-%m-%d %H:%M')} - Reservation #{instance.id} created"
            if matching_lead.notes:
                matching_lead.notes += f"\n\n{conversion_note}"
            else:
                matching_lead.notes = conversion_note
            
            matching_lead.save()
            
            # Log the conversion for debugging
            print(f"Auto-converted lead {matching_lead.id} ({matching_lead.first_name} {matching_lead.last_name}) to converted status")
