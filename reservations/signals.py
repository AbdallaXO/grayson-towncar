import logging
import time
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from .models import Reservation, Leg
from users.emails import send_reservation_confirmation
from django.db import transaction
from decimal import Decimal
from django.utils import timezone
from .models import Reservation, Lead

logger = logging.getLogger(__name__)  # Get a logger instance

# Store old values before save to compare in post_save
_lead_old_values = {}



@receiver(post_save, sender=Reservation)
def reservation_saved(sender, instance, created, **kwargs):
    """Handle reservation save events"""
    # Skip if this is a new reservation (will be handled by create_reservation)
    if created:
        from users.emails import send_internal_confirmation

        # Run in a background task or thread
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


@receiver(post_save, sender=Lead)
def sync_lead_to_ghl_on_create(sender, instance, created, **kwargs):
    """
    Automatically sync new leads to GoHighLevel when they are created.
    This syncs the contact to GHL without sending SMS initially.
    SMS can be sent later via admin actions or batch tasks.
    
    Always runs in background thread to avoid blocking the request.
    """
    if created:  # Only run when a new lead is created
        # Skip if lead doesn't have a phone number (required for GHL)
        if not instance.phone:
            logger.debug(f"Lead #{instance.id} has no phone number, skipping GHL sync")
            return
        
        # Skip if already synced (shouldn't happen on create, but safety check)
        if instance.ghl_contact_id:
            logger.debug(f"Lead #{instance.id} already has GHL contact ID, skipping sync")
            return
        
        # Always run in background thread to avoid blocking the request
        from threading import Thread
        
        def sync_ghl_in_background():
            """Sync lead to GHL in background thread"""
            local_logger = logging.getLogger(__name__)
            
            try:
                # Try Celery first (non-blocking)
                from ghl_integration.tasks import sync_lead_to_ghl_without_sms
                sync_lead_to_ghl_without_sms.delay(instance.id)
                local_logger.info(f"Queued Lead #{instance.id} for GHL sync (Celery)")
            except Exception as e:
                # If Celery fails, do direct sync in thread (still non-blocking for main request)
                local_logger.warning(f"Could not queue GHL sync task for Lead #{instance.id}: {e}. Syncing directly in thread.")
                try:
                    from ghl_integration.services import GoHighLevelService
                    
                    service = GoHighLevelService()
                    contact_id = service.create_or_update_contact(instance)
                    
                    if contact_id:
                        # Update the lead with GHL contact ID (use update to avoid signal recursion)
                        Lead.objects.filter(id=instance.id).update(
                            ghl_contact_id=contact_id,
                            ghl_synced_at=timezone.now()
                        )
                        # Also sync status to GHL for newly created contact
                        # This ensures status is synced even if it was set before contact was created
                        try:
                            service.update_contact_status_fields(
                                contact_id=contact_id,
                                status=instance.status
                            )
                            local_logger.debug(f"Synced status '{instance.status}' to GHL for newly created Lead #{instance.id}")
                        except Exception as status_error:
                            local_logger.warning(f"Failed to sync status to GHL for Lead #{instance.id}: {status_error}")
                        
                        local_logger.info(f"Successfully synced Lead #{instance.id} to GHL (contact_id: {contact_id})")
                    else:
                        local_logger.warning(f"Failed to sync Lead #{instance.id} to GHL - no contact ID returned")
                except Exception as sync_error:
                    # Don't break lead creation if GHL sync fails
                    local_logger.error(f"Error syncing Lead #{instance.id} to GHL: {sync_error}", exc_info=True)
        
        # Start background thread (non-blocking)
        thread = Thread(target=sync_ghl_in_background, daemon=True)
        thread.start()


@receiver(pre_save, sender=Lead)
def store_lead_old_values(sender, instance, **kwargs):
    """
    Store old values before save to compare in post_save signal.
    This allows us to detect if status or converted actually changed.
    """
    if instance.pk:
        try:
            old_instance = Lead.objects.get(pk=instance.pk)
            _lead_old_values[instance.pk] = {
                'status': old_instance.status,
                'ghl_contact_id': old_instance.ghl_contact_id,
            }
        except Lead.DoesNotExist:
            # New instance, no old values
            pass


@receiver(post_save, sender=Lead)
def sync_lead_status_to_ghl(sender, instance, created, **kwargs):
    """
    Sync lead status changes to GHL custom field.
    Only updates if status or converted changed and contact exists in GHL.
    Runs in background to avoid blocking Lead save.
    """
    # Skip if no GHL contact ID (contact not in GHL yet)
    if not instance.ghl_contact_id:
        return
    
    # Skip if this is a new lead (will be handled by sync_lead_to_ghl_on_create)
    if created:
        return
    
    # Get old values from pre_save signal
    old_values = _lead_old_values.get(instance.pk)
    if not old_values:
        # No old values stored, skip (might be first save or signal didn't fire)
        return
    
    # Check if status changed (converted is part of status now)
    status_changed = old_values.get('status') != instance.status
    
    # Also check if ghl_contact_id was just set (newly synced contact)
    contact_id_changed = old_values.get('ghl_contact_id') != instance.ghl_contact_id
    
    if not (status_changed or contact_id_changed):
        # No relevant changes, skip sync
        # Clean up old values
        _lead_old_values.pop(instance.pk, None)
        return
    
    # Run in background thread to avoid blocking
    from threading import Thread
    
    def sync_status_in_background():
        """Sync status to GHL in background thread"""
        local_logger = logging.getLogger(__name__)
        
        try:
            from ghl_integration.services import GoHighLevelService
            
            service = GoHighLevelService()
            success = service.update_contact_status_fields(
                contact_id=instance.ghl_contact_id,
                status=instance.status
            )
            
            if success:
                local_logger.info(
                    f"Successfully synced status '{instance.status}' to GHL for Lead #{instance.id} "
                    f"(contact_id: {instance.ghl_contact_id})"
                )
            else:
                local_logger.warning(
                    f"Failed to sync status to GHL for Lead #{instance.id} "
                    f"(contact_id: {instance.ghl_contact_id})"
                )
        except Exception as sync_error:
            # Don't break lead save if GHL sync fails
            local_logger.error(
                f"Error syncing status to GHL for Lead #{instance.id}: {sync_error}",
                exc_info=True
            )
        finally:
            # Clean up old values
            _lead_old_values.pop(instance.pk, None)
    
    # Start background thread (non-blocking)
    thread = Thread(target=sync_status_in_background, daemon=True)
    thread.start()


# ======== AUDIT LOGGING ========

# Store old values for comparison
_reservation_old_values = {}
_leg_old_values = {}


def get_request_user():
    """Get the current user from thread-local storage"""
    try:
        from reservations.middleware import get_current_user
        return get_current_user()
    except:
        pass
    return None


def get_client_ip(request):
    """Extract client IP address from request"""
    if not request:
        return None
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def create_audit_log(model_name, object_id, action, user=None, field_name=None, 
                     old_value=None, new_value=None, request=None, notes=None):
    """Helper function to create audit log entries"""
    from .models import AuditLog
    
    try:
        username = None
        if user:
            username = user.username if hasattr(user, 'username') else str(user)
        elif request and hasattr(request, 'user') and request.user.is_authenticated:
            user = request.user
            username = user.username
        
        ip_address = get_client_ip(request) if request else None
        user_agent = request.META.get('HTTP_USER_AGENT') if request else None
        
        # Truncate long values for storage
        if old_value and len(str(old_value)) > 500:
            old_value = str(old_value)[:500] + "..."
        if new_value and len(str(new_value)) > 500:
            new_value = str(new_value)[:500] + "..."
        
        AuditLog.objects.create(
            model_name=model_name,
            object_id=object_id,
            action=action,
            field_name=field_name,
            old_value=str(old_value) if old_value is not None else None,
            new_value=str(new_value) if new_value is not None else None,
            user=user,
            username=username,
            ip_address=ip_address,
            user_agent=user_agent,
            notes=notes,
        )
    except Exception as e:
        logger.error(f"Error creating audit log: {e}", exc_info=True)


@receiver(pre_save, sender=Reservation)
def store_reservation_old_values(sender, instance, **kwargs):
    """Store old values before save to compare in post_save"""
    if instance.pk:
        try:
            old_instance = Reservation.objects.get(pk=instance.pk)
            _reservation_old_values[instance.pk] = {
                'status': old_instance.status,
                'total_price': old_instance.total_price,
                'base_price': old_instance.base_price,
                'modified_by': old_instance.modified_by_id,  # Store ID to compare properly
            }
        except Reservation.DoesNotExist:
            pass


@receiver(post_save, sender=Reservation)
def log_reservation_changes(sender, instance, created, **kwargs):
    """Log reservation changes to audit log"""
    from threading import local
    
    try:
        # Try to get user from thread-local storage (set by middleware)
        user = None
        request = None
        try:
            from reservations.middleware import get_current_request, get_current_user
            request = get_current_request()
            user = get_current_user()
        except:
            pass
        
        if created:
            # New reservation created
            create_audit_log(
                model_name='Reservation',
                object_id=instance.id,
                action='created',
                user=user,
                request=request,
                notes=f"Reservation #{instance.id} created for {instance.customer.get_full_name()}"
            )
        else:
            # Reservation updated - check what changed
            old_values = _reservation_old_values.get(instance.pk, {})
            
            # Only log if something meaningful changed
            has_meaningful_change = False
            
            # Track status changes (important)
            if 'status' in old_values and old_values['status'] != instance.status:
                has_meaningful_change = True
                create_audit_log(
                    model_name='Reservation',
                    object_id=instance.id,
                    action='status_changed',
                    user=user or instance.modified_by,
                    field_name='status',
                    old_value=old_values['status'],
                    new_value=instance.status,
                    request=request,
                    notes=f"Status changed from {old_values['status']} to {instance.status}"
                )
            
            # Track significant price changes (only if change is > $1 to avoid logging tiny adjustments)
            if 'total_price' in old_values and old_values['total_price'] != instance.total_price:
                price_diff = abs(float(instance.total_price) - float(old_values['total_price']))
                if price_diff >= 1.00:  # Only log if change is $1 or more
                    has_meaningful_change = True
                    create_audit_log(
                        model_name='Reservation',
                        object_id=instance.id,
                        action='updated',
                        user=user or instance.modified_by,
                        field_name='total_price',
                        old_value=str(old_values['total_price']),
                        new_value=str(instance.total_price),
                        request=request,
                    )
            
            # Track base_price changes (for commission tracking)
            if 'base_price' in old_values and old_values['base_price'] != instance.base_price:
                price_diff = abs(float(instance.base_price) - float(old_values['base_price']))
                if price_diff >= 1.00:  # Only log if change is $1 or more
                    has_meaningful_change = True
                    create_audit_log(
                        model_name='Reservation',
                        object_id=instance.id,
                        action='updated',
                        user=user or instance.modified_by,
                        field_name='base_price',
                        old_value=str(old_values['base_price']),
                        new_value=str(instance.base_price),
                        request=request,
                    )
            
            # Only log general update if modified_by changed AND user is explicitly set
            # This prevents logging every automatic save or system update
            # Skip general updates to reduce log volume - we already track status/price changes
            # if not has_meaningful_change:
            #     old_modified_by_id = old_values.get('modified_by')
            #     new_modified_by_id = instance.modified_by_id if instance.modified_by else None
            #     if old_modified_by_id != new_modified_by_id and new_modified_by_id:
            #         # User explicitly modified - log it (but only if user is set)
            #         create_audit_log(
            #             model_name='Reservation',
            #             object_id=instance.id,
            #             action='updated',
            #             user=user or instance.modified_by,
            #             request=request,
            #             notes="Reservation modified by user"
            #         )
            
            # Clean up old values
            _reservation_old_values.pop(instance.pk, None)
    except Exception as e:
        logger.error(f"Error logging reservation changes: {e}", exc_info=True)


@receiver(pre_save, sender=Leg)
def store_leg_old_values(sender, instance, **kwargs):
    """Store old values before save to compare in post_save"""
    if instance.pk:
        try:
            old_instance = Leg.objects.get(pk=instance.pk)
            _leg_old_values[instance.pk] = {
                'driver_id': old_instance.driver_id if old_instance.driver else None,
                'status': old_instance.status,
            }
        except:
            pass


@receiver(post_save, sender=Leg)
def log_leg_changes(sender, instance, created, **kwargs):
    """Log leg changes to audit log"""
    
    try:
        # Try to get user from thread-local storage
        user = None
        request = None
        try:
            from reservations.middleware import get_current_request, get_current_user
            request = get_current_request()
            user = get_current_user()
        except:
            pass
        
        if created:
            # New leg created
            create_audit_log(
                model_name='Leg',
                object_id=instance.id,
                action='created',
                user=user,
                request=request,
                notes=f"Leg created for reservation #{instance.reservation.id}"
            )
        else:
            # Leg updated - check what changed
            old_values = _leg_old_values.get(instance.pk, {})
            
            # Track driver assignment changes
            old_driver_id = old_values.get('driver_id')
            new_driver_id = instance.driver_id if instance.driver else None
            
            if old_driver_id != new_driver_id:
                if new_driver_id:
                    action = 'driver_assigned'
                    notes = f"Driver assigned: {instance.driver}"
                else:
                    action = 'driver_unassigned'
                    notes = f"Driver unassigned (was: {old_driver_id})"
                
                create_audit_log(
                    model_name='Leg',
                    object_id=instance.id,
                    action=action,
                    user=user or instance.driver_assigned_by,
                    field_name='driver',
                    old_value=str(old_driver_id) if old_driver_id else None,
                    new_value=str(new_driver_id) if new_driver_id else None,
                    request=request,
                    notes=notes
                )
            
            # Track status changes
            if 'status' in old_values and old_values['status'] != instance.status:
                create_audit_log(
                    model_name='Leg',
                    object_id=instance.id,
                    action='status_changed',
                    user=user or instance.status_changed_by,
                    field_name='status',
                    old_value=old_values['status'],
                    new_value=instance.status,
                    request=request,
                    notes=f"Status changed from {old_values['status']} to {instance.status}"
                )
            
            # Clean up old values
            _leg_old_values.pop(instance.pk, None)
    except Exception as e:
        logger.error(f"Error logging leg changes: {e}", exc_info=True)