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

# Old values are now stored on each model instance as _pre_save_old_values
# to avoid thread-safety issues with module-level dicts.



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
    Handles commission-related fields: pending and unpaid.
    Only recalculates when commission-relevant fields actually changed.
    """
    if not instance.travel_agent:
        return

    # If update_fields is specified and doesn't include commission-relevant fields, skip
    save_update_fields = kwargs.get("update_fields")
    COMMISSION_FIELDS = {"status", "commission_amount", "commission_paid", "travel_agent"}
    if save_update_fields is not None and not COMMISSION_FIELDS.intersection(save_update_fields):
        return

    # PERF TEMP START
    _t0 = time.monotonic()
    # PERF TEMP END

    agent = instance.travel_agent
    update_fields = []

    # Track if status changed — use values already captured by
    # store_reservation_old_values pre_save (stored on instance, thread-safe)
    old_vals = getattr(instance, '_pre_save_old_values', None)
    if not created and old_vals:
        old_status = old_vals.get("status")
        status_changed = old_status != instance.status
        # If status didn't change and no explicit update_fields, skip recalc
        if not status_changed and save_update_fields is None:
            return

        # If changed to cancelled, ensure commission is properly handled
        if instance.status == "cancelled":
            logger.info(
                f"Reservation #{instance.id} cancelled - adjusting commission data"
            )
            Reservation.objects.filter(pk=instance.pk).update(
                commission_amount=Decimal("0.00")
            )
            instance.commission_amount = Decimal("0.00")

    # Calculate pending commissions (confirmed but not completed)
    pending_total = Reservation.objects.filter(
        travel_agent=agent, status="confirmed"
    ).aggregate(total=Sum("commission_amount"))["total"] or Decimal("0")

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

    if agent.unpaid_commissions != unpaid_total:
        logger.info(
            f"Updating agent {agent} unpaid commissions from ${agent.unpaid_commissions} to ${unpaid_total}"
        )
        agent.unpaid_commissions = unpaid_total
        update_fields.append("unpaid_commissions")

    if update_fields:
        agent.save(update_fields=update_fields)

        # PERF TEMP START
        logger.info(
            "PERF update_agent_commission_data: %.0fms (res #%s, agent %s)",
            (time.monotonic() - _t0) * 1000, instance.pk, agent,
        )
        # PERF TEMP END


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
    Phone matching uses last 10 digits to handle format differences
    (e.g. "(407) 555-1234" vs "4075551234" vs "+14075551234").
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

        # If no match by email, try by phone using normalized_phone index
        if not matching_lead and customer.phone_number:
            norm = Lead.normalize_phone(customer.phone_number)
            if norm:
                matching_lead = Lead.objects.filter(
                    normalized_phone=norm,
                    status__in=['new', 'contacted', 'interested', 'future_contact'],
                ).first()
        
        # If we found a matching lead, convert it
        if matching_lead:
            matching_lead.status = 'converted'
            matching_lead.converted = True
            matching_lead.converted_at = timezone.now()
            matching_lead.converted_reservation = instance

            # Add a note about the conversion
            conversion_note = f"Auto-converted on {timezone.now().strftime('%Y-%m-%d %H:%M')} - Reservation #{instance.id} created"
            if matching_lead.notes:
                matching_lead.notes += f"\n\n{conversion_note}"
            else:
                matching_lead.notes = conversion_note

            matching_lead.save()

            # Cancel any active follow-up sequence
            if matching_lead.sequence_active:
                try:
                    from ghl_integration.runner import run_in_background
                    from ghl_integration.tasks import cancel_lead_sequence
                    run_in_background(cancel_lead_sequence, matching_lead.id, reason="converted")
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Failed to queue sequence cancellation for lead #{matching_lead.id}"
                    )

            # Apply lifecycle tags for conversion (best-effort, background)
            if matching_lead.ghl_contact_id:
                try:
                    from ghl_integration.runner import run_in_background

                    def _apply_converted_tags(cid=matching_lead.ghl_contact_id, lid=matching_lead.id):
                        from ghl_integration.services import GoHighLevelService
                        from reservations.models import Lead as _Lead
                        fresh = _Lead.objects.get(id=lid)
                        GoHighLevelService().apply_lifecycle_tags(cid, fresh, "converted")

                    run_in_background(_apply_converted_tags)
                except Exception:
                    logging.getLogger(__name__).warning(
                        f"Failed to queue conversion tags for lead #{matching_lead.id}"
                    )

            # Log activity for conversion
            try:
                from ghl_integration.models import LeadActivity
                LeadActivity.objects.create(
                    lead=matching_lead,
                    activity_type=LeadActivity.ActivityType.CONVERTED,
                    description=f"Auto-converted: Reservation #{instance.id} created by {instance.customer.get_full_name()}",
                    metadata={"reservation_id": str(instance.id)},
                )
            except Exception:
                pass

            logger.info(f"Auto-converted lead {matching_lead.id} ({matching_lead.first_name} {matching_lead.last_name}) to converted status")


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
            contact_id = None

            try:
                from ghl_integration.tasks import sync_lead_to_ghl_without_sms
                result = sync_lead_to_ghl_without_sms(instance.id)
                if result and result.get("status") == "success":
                    contact_id = result.get("ghl_contact_id")
                local_logger.info(f"Synced Lead #{instance.id} to GHL")
            except Exception as e:
                local_logger.warning(f"GHL sync task failed for Lead #{instance.id}: {e}. Trying direct sync.")
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
                        try:
                            service.update_contact_status_fields(
                                contact_id=contact_id,
                                status=instance.status
                            )
                        except Exception as status_error:
                            local_logger.warning(f"Failed to sync status to GHL for Lead #{instance.id}: {status_error}")

                        local_logger.info(f"Successfully synced Lead #{instance.id} to GHL (contact_id: {contact_id})")
                    else:
                        local_logger.warning(f"Failed to sync Lead #{instance.id} to GHL - no contact ID returned")
                except Exception as sync_error:
                    local_logger.error(f"Error syncing Lead #{instance.id} to GHL: {sync_error}", exc_info=True)

            # Apply lifecycle tags for new lead (best-effort)
            if contact_id:
                try:
                    from ghl_integration.services import GoHighLevelService
                    # Refresh lead to get latest data
                    fresh_lead = Lead.objects.get(id=instance.id)
                    GoHighLevelService().apply_lifecycle_tags(contact_id, fresh_lead, "created")
                except Exception as tag_err:
                    local_logger.warning(f"Failed to apply created tags for Lead #{instance.id}: {tag_err}")

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
            instance._pre_save_old_values = {
                'status': old_instance.status,
                'ghl_contact_id': old_instance.ghl_contact_id,
            }
        except Lead.DoesNotExist:
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
    
    # Get old values from pre_save signal (stored on instance, thread-safe)
    old_values = getattr(instance, '_pre_save_old_values', None)
    if not old_values:
        # No old values stored, skip (might be first save or signal didn't fire)
        return

    # Check if status changed (converted is part of status now)
    status_changed = old_values.get('status') != instance.status

    # Also check if ghl_contact_id was just set (newly synced contact)
    contact_id_changed = old_values.get('ghl_contact_id') != instance.ghl_contact_id

    if not (status_changed or contact_id_changed):
        # No relevant changes, skip sync
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
            # Clean up old values from instance
            if hasattr(instance, '_pre_save_old_values'):
                del instance._pre_save_old_values
    
    # Start background thread (non-blocking)
    thread = Thread(target=sync_status_in_background, daemon=True)
    thread.start()


# ======== AUDIT LOGGING ========

# Old values stored on instance._pre_save_old_values (thread-safe)


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
        # Resolve user: AnonymousUser and non-User objects must be treated as None
        if user and hasattr(user, 'is_authenticated') and not user.is_authenticated:
            user = None
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
            instance._pre_save_old_values = {
                'status': old_instance.status,
                'total_price': old_instance.total_price,
                'base_price': old_instance.base_price,
                'modified_by': old_instance.modified_by_id,
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
            old_values = getattr(instance, '_pre_save_old_values', {})
            
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
            
            # Clean up old values from instance
            if hasattr(instance, '_pre_save_old_values'):
                del instance._pre_save_old_values
    except Exception as e:
        logger.error(f"Error logging reservation changes: {e}", exc_info=True)


@receiver(pre_save, sender=Leg)
def store_leg_old_values(sender, instance, **kwargs):
    """Store old values before save to compare in post_save.
    Skips the DB fetch when update_fields is specified and neither 'status' nor 'driver'
    are being updated, avoiding one extra SELECT per save (e.g. confirmation_sms_sent_at saves).
    """
    if not instance.pk:
        return
    update_fields = kwargs.get('update_fields')
    if update_fields is not None and 'status' not in update_fields and 'driver' not in update_fields:
        return
    try:
        old_vals = Leg.objects.filter(pk=instance.pk).values('status', 'driver_id').first()
        if old_vals:
            instance._pre_save_old_values = {
                'driver_id': old_vals['driver_id'],
                'status': old_vals['status'],
            }
    except Exception:
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
            old_values = getattr(instance, '_pre_save_old_values', {})
            
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
            
            # Clean up old values from instance
            if hasattr(instance, '_pre_save_old_values'):
                del instance._pre_save_old_values
    except Exception as e:
        logger.error(f"Error logging leg changes: {e}", exc_info=True)