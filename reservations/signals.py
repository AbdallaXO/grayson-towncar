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



@receiver(post_save, sender=Reservation)
def reservation_saved(sender, instance, created, **kwargs):
    """Handle reservation save events"""
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
                    f"Processing reservation #{instance.id} (attempt {attempt}/{max_attempts})"
                )

                # HubSpot sync removed - no longer needed
                success = True
                break


        # Start a background thread to handle these tasks
        thread = Thread(target=background_tasks)
        thread.daemon = True  # Thread will exit when main thread exits
        thread.start()

        return

    # HubSpot integration removed - no longer syncing to HubSpot


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
