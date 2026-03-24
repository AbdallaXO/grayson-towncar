"""
Tasks for GoHighLevel SMS automation and follow-up engine.

Tasks:
- sync_lead_to_ghl_and_send_sms: Sync lead + send initial SMS (existing)
- batch_send_unsent_leads: Batch for unsent initial SMS (existing)
- sync_lead_to_ghl_without_sms: Sync contact only (existing)
- start_follow_up_sequence: Schedule all 5 follow-up steps for a lead
- process_follow_up_batch: Core engine — sends due follow-ups every 30 min
- cancel_lead_sequence: Cancel all pending tasks for a lead
"""

import time as time_module
from datetime import timedelta

from django.utils import timezone
from django.db import transaction
from django.db.models import Q
import logging

logger = logging.getLogger(__name__)


def sync_lead_to_ghl_and_send_sms(lead_id, _attempt=1, _max_retries=3):
    """
    Sync a single lead to GHL and send initial SMS.
    
    This task:
    - Gets the lead from database
    - Skips if initial_sms_sent=True or no phone number
    - Creates/updates contact in GHL
    - Generates and sends SMS message
    - Updates lead fields in database
    
    Args:
        lead_id: ID of the Lead to sync
        
    Returns:
        dict: Status and details of the operation
    """
    from reservations.models import Lead
    from .services import GoHighLevelService, get_sms_template
    from .timing import is_within_send_window

    # Double-check send window — don't text people at night
    if not is_within_send_window():
        logger.info(f"Lead #{lead_id}: outside send window, will retry next batch")
        return {"status": "outside_window", "lead_id": lead_id}

    try:
        # Atomically claim this lead to prevent duplicate SMS.
        # Two threads/workers can both call this for the same lead; the first
        # one sets initial_sms_sent=True inside the lock and proceeds, the
        # second sees it's already claimed and skips.
        with transaction.atomic():
            lead = Lead.objects.select_for_update().get(id=lead_id)

            if lead.initial_sms_sent or lead.initial_email_sent:
                logger.info(f"Lead #{lead_id} already contacted, skipping")
                return {"status": "skipped", "reason": "already_sent", "lead_id": lead_id}

            if lead.converted or lead.status == Lead.StatusChoices.CONVERTED:
                logger.info(f"Lead #{lead_id} already converted, skipping SMS")
                return {"status": "skipped", "reason": "already_converted", "lead_id": lead_id}

            # Skip if another lead with same phone already has an active sequence
            # or was already contacted (prevents spamming the same person twice)
            if lead.phone:
                norm = Lead.normalize_phone(lead.phone)
                if norm:
                    dupe = Lead.objects.filter(
                        normalized_phone=norm,
                        initial_sms_sent=True,
                    ).exclude(id=lead.id).only("id").first()
                    if dupe:
                        logger.info(
                            f"Lead #{lead_id} shares phone with Lead #{dupe.id} "
                            f"(already contacted), skipping duplicate SMS"
                        )
                        lead.initial_sms_sent = True  # Mark so it doesn't get retried
                        lead.initial_sms_sent_at = timezone.now()
                        lead.save(update_fields=["initial_sms_sent", "initial_sms_sent_at"])
                        return {"status": "skipped", "reason": "duplicate_phone", "lead_id": lead_id}

            if not lead.phone:
                # No phone number — go straight to email fallback
                logger.info(f"Lead #{lead_id} has no phone number, trying email fallback")
                email_sent = _try_email_fallback(lead)
                if not email_sent:
                    _mark_lead_lost(lead, "No phone number and email failed")
                return {
                    "status": "email_fallback" if email_sent else "lost",
                    "lead_id": lead_id,
                }

            # Claim: mark as sent NOW so no other thread picks it up
            lead.initial_sms_sent = True
            lead.initial_sms_sent_at = timezone.now()
            lead.save(update_fields=["initial_sms_sent", "initial_sms_sent_at"])

        # Outside the lock: do the actual API calls
        from .services import log_sync_start, log_sync_success, log_sync_failure
        service = GoHighLevelService()

        # Step 1: Create/update contact in GHL
        contact_log = log_sync_start(lead, "create_contact")
        contact_id = service.create_or_update_contact(lead)

        if not contact_id:
            log_sync_failure(contact_log, "create_or_update_contact returned None")
            logger.error(f"Failed to create/update GHL contact for lead #{lead_id}")
            raise Exception("Failed to create/update GHL contact")
        log_sync_success(contact_log, {"contact_id": contact_id})

        # Step 2: Generate and send SMS
        message = get_sms_template(lead)
        sms_log = log_sync_start(lead, "send_sms", {"message": message, "contact_id": contact_id})
        sms_sent = service.send_sms(contact_id, message)

        if not sms_sent:
            log_sync_failure(sms_log, "send_sms returned False")
            logger.warning(f"SMS failed for lead #{lead_id}, trying email fallback")
            # SMS failed (bad number, UK number, etc.) — try email instead
            email_sent = _try_email_fallback(lead)
            if email_sent:
                # No SMS sequence for email-only leads — flag for human follow-up
                with transaction.atomic():
                    lead.refresh_from_db()
                    lead.needs_human_follow_up = True
                    lead.save(update_fields=["needs_human_follow_up"])
                logger.info(f"Email-only Lead #{lead_id} flagged for human follow-up")
                return {
                    "status": "email_fallback",
                    "lead_id": lead_id,
                    "ghl_contact_id": contact_id,
                }
            else:
                # Both SMS and email failed — mark as lost
                _mark_lead_lost(lead, "Both SMS and email failed")
                return {"status": "lost", "lead_id": lead_id}

        log_sync_success(sms_log)

        # Step 3: Update Django lead (already claimed initial_sms_sent above)
        with transaction.atomic():
            lead.refresh_from_db()
            lead.ghl_contact_id = contact_id
            lead.ghl_synced_at = timezone.now()
            lead.status = Lead.StatusChoices.CONTACTED
            lead.contact_attempts = (lead.contact_attempts or 0) + 1
            lead.last_contact_date = timezone.now()
            lead.save(update_fields=[
                'ghl_contact_id',
                'ghl_synced_at',
                'status',
                'contact_attempts',
                'last_contact_date'
            ])

        logger.info(f"Successfully synced Lead #{lead_id} to GHL, sent SMS, and marked as CONTACTED (contact_id: {contact_id})")

        # Apply lifecycle tags (best-effort)
        try:
            service.apply_lifecycle_tags(contact_id, lead, "sms_sent")
        except Exception as tag_err:
            logger.warning(f"Failed to apply sms_sent tags for Lead #{lead_id}: {tag_err}")

        # Start the follow-up sequence (steps 2-5) now that Step 1 is sent
        try:
            from .runner import run_in_background
            run_in_background(start_follow_up_sequence, lead_id)
            logger.info(f"Queued follow-up sequence for Lead #{lead_id}")
        except Exception as seq_err:
            logger.warning(f"Failed to queue follow-up sequence for Lead #{lead_id}: {seq_err}")

        return {
            "status": "success",
            "lead_id": lead_id,
            "ghl_contact_id": contact_id,
            "sms_sent": sms_sent
        }

    except Lead.DoesNotExist:
        logger.error(f"Lead #{lead_id} not found")
        return {"status": "error", "reason": "lead_not_found", "lead_id": lead_id}
    except Exception as e:
        logger.error(f"Error syncing Lead #{lead_id} to GHL (attempt {_attempt}/{_max_retries}): {e}", exc_info=True)
        if _attempt < _max_retries:
            delay = 60 * _attempt  # Backoff: 60s, 120s, 180s
            logger.info(f"Retrying Lead #{lead_id} in {delay}s (attempt {_attempt + 1})")
            time_module.sleep(delay)
            return sync_lead_to_ghl_and_send_sms(lead_id, _attempt=_attempt + 1, _max_retries=_max_retries)
        # All retries exhausted — try email fallback before giving up
        try:
            lead = Lead.objects.get(id=lead_id)
            email_sent = _try_email_fallback(lead)
            if email_sent:
                return {"status": "email_fallback", "lead_id": lead_id}
            _mark_lead_lost(lead, "SMS retries exhausted, email also failed")
        except Lead.DoesNotExist:
            pass
        return {"status": "lost", "reason": "all_channels_failed", "lead_id": lead_id}


def _try_email_fallback(lead):
    """
    Try sending a quote email when SMS fails (bad number, UK number, etc.).
    Updates the lead's initial_email_sent fields on success.

    Returns:
        bool: True if email was sent, False otherwise
    """
    from users.emails import send_lead_quote_email

    if not lead.email:
        logger.warning(f"Lead #{lead.id}: no email either, cannot fall back")
        return False

    try:
        email_sent = send_lead_quote_email(lead)
        if email_sent:
            lead.initial_email_sent = True
            lead.initial_email_sent_at = timezone.now()
            lead.status = lead.StatusChoices.CONTACTED
            lead.contact_attempts = (lead.contact_attempts or 0) + 1
            lead.last_contact_date = timezone.now()
            lead.save(update_fields=[
                "initial_email_sent", "initial_email_sent_at",
                "status", "contact_attempts", "last_contact_date",
            ])
            logger.info(f"Lead #{lead.id}: email fallback sent successfully")
            return True
        return False
    except Exception as e:
        logger.error(f"Lead #{lead.id}: email fallback failed: {e}", exc_info=True)
        return False


def _mark_lead_lost(lead, reason):
    """Mark a lead as lost when all contact channels have failed."""
    from .models import LeadActivity

    lead.status = lead.StatusChoices.LOST
    lead.initial_sms_sent = False
    lead.initial_sms_sent_at = None
    lead.save(update_fields=["status", "initial_sms_sent", "initial_sms_sent_at"])

    LeadActivity.objects.create(
        lead=lead,
        activity_type=LeadActivity.ActivityType.STATUS_CHANGE,
        description=f"Marked as lost: {reason}",
    )
    logger.warning(f"Lead #{lead.id} marked as lost: {reason}")


def batch_send_unsent_leads():
    """
    Batch task to find all leads without SMS sent and queue them for sending.

    Respects the send window (8 AM – 9 PM Eastern). Outside the window,
    skips the batch so leads queue up and get sent the next morning.

    Returns:
        dict: Count of queued leads
    """
    from reservations.models import Lead
    from .runner import run_in_background
    from .timing import is_within_send_window

    # Don't send texts in the middle of the night
    if not is_within_send_window():
        logger.info("Outside send window (8 AM - 9 PM Eastern), skipping initial SMS batch")
        return {"status": "outside_window", "queued": 0}

    # Get leads that haven't been contacted by any channel yet.
    # Only NEW leads — not converted, lost, or already contacted.
    unsent_leads = Lead.objects.filter(
        initial_sms_sent=False,
        initial_email_sent=False,
        status=Lead.StatusChoices.NEW,
        converted=False,
    ).filter(
        Q(phone__isnull=False) & ~Q(phone="") | Q(email__isnull=False) & ~Q(email="")
    ).values_list('id', flat=True)[:50]  # Limit batch size

    count = 0
    for lead_id in unsent_leads:
        run_in_background(sync_lead_to_ghl_and_send_sms, lead_id)
        count += 1

    logger.info(f"Queued {count} leads for GHL sync and SMS")

    # Also rescue stale leads (contacted but no sequence, no reply, have email)
    stale_rescued = _rescue_stale_leads()

    return {"queued": count, "stale_rescued": stale_rescued}


def _rescue_stale_leads():
    """
    Find stale leads (contacted, no sequence, no reply, not converted)
    that have an email address but were never emailed.
    Send them the initial quote email and flag for human follow-up.
    Runs as part of the regular batch cycle, 50 at a time.
    """
    from reservations.models import Lead

    stale = Lead.objects.filter(
        status__in=[Lead.StatusChoices.NEW, Lead.StatusChoices.CONTACTED],
        sequence_active=False,
        converted=False,
        has_replied=False,
        initial_email_sent=False,
    ).exclude(email__isnull=True).exclude(email="").order_by("created_at")[:50]

    rescued = 0
    for lead in stale:
        try:
            if _try_email_fallback(lead):
                lead.refresh_from_db()
                lead.needs_human_follow_up = True
                lead.save(update_fields=["needs_human_follow_up"])
                rescued += 1
                logger.info(f"Rescued stale Lead #{lead.id} via email")
            else:
                logger.warning(f"Stale Lead #{lead.id}: email send failed")
        except Exception as e:
            logger.error(f"Error rescuing stale Lead #{lead.id}: {e}")

    if rescued:
        logger.info(f"Rescued {rescued} stale leads via email")
    return rescued


def sync_lead_to_ghl_without_sms(lead_id):
    """
    Sync a single lead to GHL without sending SMS.
    
    Useful for importing existing leads or syncing contact information
    without triggering an SMS message.
    
    Args:
        lead_id: ID of the Lead to sync
        
    Returns:
        dict: Status and details of the operation
    """
    from reservations.models import Lead
    from .services import GoHighLevelService, log_sync_start, log_sync_success, log_sync_failure

    try:
        lead = Lead.objects.get(id=lead_id)

        # Skip if no phone
        if not lead.phone:
            logger.warning(f"Lead #{lead_id} has no phone number")
            return {"status": "skipped", "reason": "no_phone", "lead_id": lead_id}

        service = GoHighLevelService()

        # Create/update contact in GHL
        sync_log = log_sync_start(lead, "create_contact")
        contact_id = service.create_or_update_contact(lead)

        if not contact_id:
            log_sync_failure(sync_log, "create_or_update_contact returned None")
            logger.error(f"Failed to create/update GHL contact for lead #{lead_id}")
            return {"status": "error", "reason": "failed_to_sync", "lead_id": lead_id}
        log_sync_success(sync_log, {"contact_id": contact_id})
        
        # Update Django lead within transaction
        with transaction.atomic():
            lead.ghl_contact_id = contact_id
            lead.ghl_synced_at = timezone.now()
            lead.save(update_fields=['ghl_contact_id', 'ghl_synced_at'])
        
        # Also sync status to GHL if contact was just created/synced
        # This ensures status is synced even if it was changed before ghl_contact_id was set
        try:
            from ghl_integration.services import GoHighLevelService
            service = GoHighLevelService()
            service.update_contact_status_fields(
                contact_id=contact_id,
                status=lead.status
            )
            logger.debug(f"Synced status '{lead.status}' to GHL for newly synced Lead #{lead_id}")
        except Exception as status_sync_error:
            # Don't fail the whole sync if status update fails
            logger.warning(f"Failed to sync status to GHL for Lead #{lead_id}: {status_sync_error}")
        
        logger.info(f"Successfully synced Lead #{lead_id} to GHL without SMS (contact_id: {contact_id})")
        
        return {
            "status": "success",
            "lead_id": lead_id,
            "ghl_contact_id": contact_id
        }
        
    except Lead.DoesNotExist:
        logger.error(f"Lead #{lead_id} not found")
        return {"status": "error", "reason": "lead_not_found", "lead_id": lead_id}
    except Exception as e:
        logger.error(f"Error syncing Lead #{lead_id} to GHL: {e}", exc_info=True)
        return {"status": "error", "reason": "internal_error", "lead_id": lead_id}


# ---------------------------------------------------------------------------
# Follow-Up Engine Tasks
# ---------------------------------------------------------------------------

# Step delays in hours relative to Step 1 send time
STEP_DELAYS = {
    1: 0,
    2: 4,
    3: 20,
    4: 48,   # 2 days
    5: 96,   # 4 days
}


def start_follow_up_sequence(lead_id):
    """
    Schedule all 5 follow-up steps for a lead.

    Called after initial SMS is sent successfully.
    All timing is relative to when Step 1 (initial SMS) was sent.
    """
    from reservations.models import Lead
    from .models import FollowUpTask, FollowUpSequence, LeadActivity
    from .segmentation import classify_lead
    from .timing import adjust_to_send_window

    try:
        lead = Lead.objects.get(id=lead_id)

        # Guard: don't start if already active, converted, or no phone
        if lead.sequence_active:
            logger.info(f"Lead #{lead_id} already has active sequence, skipping")
            return {"status": "skipped", "reason": "already_active"}

        # Guard: don't start if another lead with the same phone already has
        # an active sequence (prevents double-texting round-trip leads)
        if lead.phone:
            digits = ''.join(filter(str.isdigit, lead.phone))
            if len(digits) >= 10:
                last10 = digits[-10:]
                other_active = Lead.objects.filter(
                    sequence_active=True,
                ).exclude(id=lead.id).exclude(phone__isnull=True).exclude(phone="")
                for other in other_active:
                    other_digits = ''.join(filter(str.isdigit, other.phone))
                    if len(other_digits) >= 10 and other_digits[-10:] == last10:
                        logger.info(
                            f"Lead #{lead_id} shares phone with Lead #{other.id} "
                            f"(active sequence), skipping duplicate sequence"
                        )
                        return {"status": "skipped", "reason": "duplicate_phone_sequence"}

        if lead.converted:
            logger.info(f"Lead #{lead_id} is already converted, skipping")
            return {"status": "skipped", "reason": "converted"}
        if not lead.phone:
            logger.info(f"Lead #{lead_id} has no phone, skipping sequence")
            return {"status": "skipped", "reason": "no_phone"}

        # Classify the lead
        segment = classify_lead(lead)
        lead.segment = segment
        lead.sequence_active = True
        lead.save(update_fields=["segment", "sequence_active"])

        # Base time = when Step 1 was sent (initial_sms_sent_at)
        # If not set yet, use now (sequence start triggered before SMS in some flows)
        base_time = lead.initial_sms_sent_at or timezone.now()

        # Create FollowUpTask for steps 2-5
        # (Step 1 is the initial SMS already sent by sync_lead_to_ghl_and_send_sms)
        tasks_created = 0
        for step in range(2, 6):
            scheduled_raw = base_time + timedelta(hours=STEP_DELAYS[step])
            scheduled_at = adjust_to_send_window(scheduled_raw)

            FollowUpTask.objects.update_or_create(
                lead=lead,
                step_number=step,
                defaults={
                    "segment": segment,
                    "status": FollowUpTask.StatusChoices.PENDING,
                    "scheduled_at": scheduled_at,
                },
            )
            tasks_created += 1

        # Also create a Step 1 record for audit trail (already sent)
        FollowUpTask.objects.update_or_create(
            lead=lead,
            step_number=1,
            defaults={
                "segment": segment,
                "status": FollowUpTask.StatusChoices.SENT,
                "scheduled_at": base_time,
                "sent_at": base_time,
                "message_body": "(initial SMS — sent via sync_lead_to_ghl_and_send_sms)",
            },
        )

        # Log activity
        LeadActivity.objects.create(
            lead=lead,
            activity_type=LeadActivity.ActivityType.SEQUENCE_STARTED,
            description=f"Follow-up sequence started with segment '{segment}'. Steps 2-5 scheduled.",
            metadata={"segment": segment, "base_time": str(base_time), "tasks_created": tasks_created},
        )

        logger.info(f"Started follow-up sequence for Lead #{lead_id} (segment={segment}, {tasks_created} tasks)")
        return {"status": "success", "lead_id": lead_id, "segment": segment, "tasks_created": tasks_created}

    except Lead.DoesNotExist:
        logger.error(f"Lead #{lead_id} not found for sequence start")
        return {"status": "error", "reason": "lead_not_found"}
    except Exception as e:
        logger.error(f"Error starting sequence for Lead #{lead_id}: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}


def process_follow_up_batch():
    """
    Core follow-up engine. Runs every 30 minutes via Celery beat.

    Finds all PENDING FollowUpTasks whose scheduled_at <= now,
    checks stop conditions and send window AT THE MOMENT OF SENDING,
    then sends via GHL.

    Cap: 100 tasks per batch. Priority ordering: URGENT leads first.
    """
    from reservations.models import Lead
    from .models import FollowUpTask, FollowUpSequence, LeadActivity
    from .services import GoHighLevelService
    from .templates_engine import render_follow_up_message
    from .timing import is_within_send_window, next_morning_slot

    now = timezone.now()

    # CRITICAL: Check send window at the moment of sending
    if not is_within_send_window(now):
        logger.info("Outside send window (8 AM - 9 PM Eastern), skipping batch")
        return {"status": "outside_window", "processed": 0}

    # Collect due task IDs first (lightweight query).
    # We'll lock each row individually below to prevent duplicate sends
    # when multiple Gunicorn workers run the scheduler concurrently.
    due_task_ids = list(
        FollowUpTask.objects.filter(
            status=FollowUpTask.StatusChoices.PENDING,
            scheduled_at__lte=now,
        )
        .order_by("lead__priority", "scheduled_at")
        .values_list("id", flat=True)[:100]
    )

    if not due_task_ids:
        logger.debug("No due follow-up tasks found")
        return {"status": "no_tasks", "processed": 0}

    service = GoHighLevelService()
    sent = 0
    cancelled = 0
    failed = 0
    rescheduled = 0

    for task_id in due_task_ids:
        # --- Atomically claim this task so no other worker can process it ---
        try:
            with transaction.atomic():
                task = (
                    FollowUpTask.objects
                    .select_for_update(skip_locked=True)
                    .select_related("lead")
                    .get(id=task_id, status=FollowUpTask.StatusChoices.PENDING)
                )
        except FollowUpTask.DoesNotExist:
            # Already claimed by another worker or no longer PENDING
            continue

        lead = task.lead

        # --- Stop condition checks (fresh from DB via select_related) ---

        # 1. Lead has replied (DB flag)
        if lead.has_replied:
            _cancel_task(task, "replied", now)
            cancelled += 1
            continue

        # 2. Lead is converted
        if lead.converted or lead.status == Lead.StatusChoices.CONVERTED:
            _cancel_task(task, "converted", now)
            cancelled += 1
            continue

        # 3. Trip date has passed
        if lead.pickup_date and lead.pickup_date < now.date():
            _cancel_task(task, "expired_date", now)
            cancelled += 1
            continue

        # 4. Safety net: check GHL conversation for inbound replies.
        #    Catches replies even when the webhook fails to fire.
        if lead.ghl_contact_id and not lead.has_replied:
            try:
                if service.contact_has_replied(lead.ghl_contact_id):
                    # Update lead so future checks skip the API call
                    lead.has_replied = True
                    lead.needs_human_follow_up = True
                    lead.save(update_fields=["has_replied", "needs_human_follow_up"])
                    _cancel_task(task, "replied", now)
                    # Cancel remaining pending tasks for this lead
                    from .tasks import cancel_lead_sequence
                    cancel_lead_sequence(lead.id, reason="replied")
                    cancelled += 1
                    logger.warning(
                        f"Reply detected via GHL API for lead #{lead.id} "
                        f"(webhook missed it) — cancelling sequence"
                    )
                    continue
            except Exception as e:
                logger.warning(f"GHL reply check failed for lead #{lead.id}: {e}")

        # --- Send window double-check at exact moment ---
        if not is_within_send_window():
            # Window closed during batch processing
            task.scheduled_at = next_morning_slot()
            task.save(update_fields=["scheduled_at"])
            rescheduled += 1
            continue

        # --- Ensure GHL contact exists ---
        if not lead.ghl_contact_id:
            try:
                contact_id = service.create_or_update_contact(lead)
                if contact_id:
                    lead.ghl_contact_id = contact_id
                    lead.ghl_synced_at = timezone.now()
                    lead.save(update_fields=["ghl_contact_id", "ghl_synced_at"])
                else:
                    task.attempts += 1
                    if task.attempts >= 3:
                        task.status = FollowUpTask.StatusChoices.FAILED
                    task.save(update_fields=["attempts", "status"])
                    failed += 1
                    continue
            except Exception as e:
                logger.error(f"Failed to create GHL contact for lead #{lead.id}: {e}")
                task.attempts += 1
                task.save(update_fields=["attempts"])
                failed += 1
                continue

        # --- Render message ---
        template_row = FollowUpSequence.objects.filter(
            step_number=task.step_number,
            segment=task.segment,
            is_active=True,
        ).first()

        # Fall back to "general" segment if no segment-specific template
        if not template_row:
            template_row = FollowUpSequence.objects.filter(
                step_number=task.step_number,
                segment="general",
                is_active=True,
            ).first()

        if not template_row:
            logger.warning(f"No template found for step {task.step_number}, segment {task.segment}")
            task.status = FollowUpTask.StatusChoices.SKIPPED
            task.save(update_fields=["status"])
            continue

        message = render_follow_up_message(template_row.message_template, lead)

        # --- Send via GHL ---
        try:
            sms_success = service.send_sms(lead.ghl_contact_id, message)
        except Exception as e:
            logger.error(f"SMS send error for lead #{lead.id} step {task.step_number}: {e}")
            sms_success = False

        if sms_success:
            with transaction.atomic():
                task.status = FollowUpTask.StatusChoices.SENT
                task.sent_at = timezone.now()
                task.message_body = message
                task.save(update_fields=["status", "sent_at", "message_body"])

                lead.contact_attempts = (lead.contact_attempts or 0) + 1
                lead.last_contact_date = timezone.now()
                lead.save(update_fields=["contact_attempts", "last_contact_date"])

            LeadActivity.objects.create(
                lead=lead,
                activity_type=LeadActivity.ActivityType.SMS_SENT,
                description=f"Follow-up step {task.step_number} sent",
                metadata={"step": task.step_number, "segment": task.segment},
            )

            # After step 5: mark sequence complete (do NOT mark cold)
            if task.step_number == 5:
                lead.sequence_active = False
                lead.sequence_completed_at = timezone.now()
                lead.save(update_fields=["sequence_active", "sequence_completed_at"])

                LeadActivity.objects.create(
                    lead=lead,
                    activity_type=LeadActivity.ActivityType.SEQUENCE_COMPLETED,
                    description="Follow-up sequence completed (5 steps). Status left for human decision.",
                )

            sent += 1
        else:
            task.attempts += 1
            if task.attempts >= 3:
                task.status = FollowUpTask.StatusChoices.FAILED
                LeadActivity.objects.create(
                    lead=lead,
                    activity_type=LeadActivity.ActivityType.SMS_SENT,
                    description=f"Follow-up step {task.step_number} failed after {task.attempts} attempts",
                    metadata={"step": task.step_number, "attempts": task.attempts},
                )
            task.save(update_fields=["attempts", "status"])
            failed += 1

        # Rate limiting: 1 second between sends
        time_module.sleep(1)

    logger.info(f"Follow-up batch complete: {sent} sent, {cancelled} cancelled, {failed} failed, {rescheduled} rescheduled")
    return {"sent": sent, "cancelled": cancelled, "failed": failed, "rescheduled": rescheduled}


def _cancel_task(task, reason, now):
    """Helper to cancel a single FollowUpTask and log it."""
    from .models import LeadActivity

    task.status = task.StatusChoices.CANCELLED
    task.cancelled_at = now
    task.cancel_reason = reason
    task.save(update_fields=["status", "cancelled_at", "cancel_reason"])


def cancel_lead_sequence(lead_id, reason="manual"):
    """
    Cancel all pending follow-up tasks for a lead.

    Called when:
    - Lead replies (reason="replied")
    - Lead converts to reservation (reason="converted")
    - Lead's trip date passes (reason="expired_date")
    - Manual admin action (reason="manual")
    """
    from reservations.models import Lead
    from .models import FollowUpTask, LeadActivity

    now = timezone.now()

    cancelled_count = FollowUpTask.objects.filter(
        lead_id=lead_id,
        status=FollowUpTask.StatusChoices.PENDING,
    ).update(
        status=FollowUpTask.StatusChoices.CANCELLED,
        cancelled_at=now,
        cancel_reason=reason,
    )

    # Update lead
    try:
        lead = Lead.objects.get(id=lead_id)
        lead.sequence_active = False
        update_fields = ["sequence_active"]

        if reason == "replied":
            lead.needs_human_follow_up = True
            update_fields.append("needs_human_follow_up")

        lead.save(update_fields=update_fields)
    except Lead.DoesNotExist:
        pass

    if cancelled_count > 0:
        LeadActivity.objects.create(
            lead_id=lead_id,
            activity_type=LeadActivity.ActivityType.SEQUENCE_STOPPED,
            description=f"Sequence cancelled: {reason}. {cancelled_count} pending task(s) cancelled.",
            metadata={"reason": reason, "cancelled_count": cancelled_count},
        )

    logger.info(f"Cancelled {cancelled_count} pending tasks for Lead #{lead_id} (reason={reason})")
    return {"lead_id": lead_id, "cancelled": cancelled_count, "reason": reason}


# ---------------------------------------------------------------------------
# GHL Sync Reliability Tasks (Phase 3)
# ---------------------------------------------------------------------------

def retry_failed_syncs():
    """
    Retry failed GHL sync operations that are due for retry.
    Runs every 15 minutes (called twice per scheduler cycle).

    Picks up GHLSyncLog entries with status=FAILED and next_retry_at <= now,
    re-attempts the operation, and updates the log accordingly.
    """
    from .models import GHLSyncLog
    from .services import GoHighLevelService, log_sync_success, log_sync_failure

    now = timezone.now()
    failed_logs = GHLSyncLog.objects.filter(
        status=GHLSyncLog.StatusChoices.FAILED,
        next_retry_at__lte=now,
    ).select_related("lead")[:50]

    if not failed_logs:
        return {"retried": 0}

    service = GoHighLevelService()
    retried = 0
    succeeded = 0
    still_failed = 0

    for sync_log in failed_logs:
        lead = sync_log.lead
        try:
            success = False

            if sync_log.action == GHLSyncLog.ActionChoices.CREATE_CONTACT:
                contact_id = service.create_or_update_contact(lead)
                if contact_id:
                    lead.ghl_contact_id = contact_id
                    lead.ghl_synced_at = timezone.now()
                    lead.save(update_fields=["ghl_contact_id", "ghl_synced_at"])
                    success = True

            elif sync_log.action == GHLSyncLog.ActionChoices.SEND_SMS:
                if lead.ghl_contact_id and sync_log.request_payload:
                    message = sync_log.request_payload.get("message", "")
                    if message:
                        success = service.send_sms(lead.ghl_contact_id, message)

            elif sync_log.action == GHLSyncLog.ActionChoices.UPDATE_STATUS:
                if lead.ghl_contact_id:
                    success = service.update_contact_status_fields(
                        lead.ghl_contact_id, lead.status
                    )

            elif sync_log.action in (
                GHLSyncLog.ActionChoices.ADD_TAG,
                GHLSyncLog.ActionChoices.REMOVE_TAG,
            ):
                if lead.ghl_contact_id and sync_log.request_payload:
                    tag = sync_log.request_payload.get("tag", "")
                    if tag:
                        if sync_log.action == GHLSyncLog.ActionChoices.ADD_TAG:
                            success = service.add_tag(lead.ghl_contact_id, tag)
                        else:
                            success = service.remove_tag(lead.ghl_contact_id, tag)

            if success:
                log_sync_success(sync_log)
                succeeded += 1
            else:
                log_sync_failure(sync_log, "Retry returned False/None")
                still_failed += 1

        except Exception as e:
            log_sync_failure(sync_log, str(e))
            still_failed += 1

        retried += 1
        time_module.sleep(0.5)  # Rate limiting

    logger.info(
        f"Retry batch: {retried} retried, {succeeded} succeeded, {still_failed} still failed"
    )
    return {"retried": retried, "succeeded": succeeded, "still_failed": still_failed}


def alert_dead_letter_syncs():
    """
    Alert on dead-letter GHL sync operations via ntfy.
    Runs every 6 hours.
    """
    from .models import GHLSyncLog

    dead_letters = GHLSyncLog.objects.filter(
        status=GHLSyncLog.StatusChoices.DEAD_LETTER,
        resolved_at__isnull=True,
    ).count()

    if dead_letters == 0:
        return {"dead_letters": 0}

    # Get some details for the alert
    recent = GHLSyncLog.objects.filter(
        status=GHLSyncLog.StatusChoices.DEAD_LETTER,
        resolved_at__isnull=True,
    ).select_related("lead").order_by("-created_at")[:5]

    details = []
    for log in recent:
        lead_name = f"{log.lead.first_name} {log.lead.last_name}".strip() or f"Lead #{log.lead_id}"
        details.append(f"- {log.get_action_display()}: {lead_name} ({log.error_message[:60]})")

    detail_text = "\n".join(details)

    try:
        from reservations.utils import send_ntfy_notification
        send_ntfy_notification(
            title=f"GHL Sync: {dead_letters} Dead Letter(s)",
            message=f"{dead_letters} GHL sync operations have permanently failed and need manual review.\n\nRecent failures:\n{detail_text}",
            priority="high",
            tags=["warning", "ghl", "sync"],
        )
    except Exception as e:
        logger.error(f"Failed to send dead letter alert: {e}")

    logger.warning(f"GHL dead letter alert: {dead_letters} unresolved entries")
    return {"dead_letters": dead_letters}


# ---------------------------------------------------------------------------
# Lost Lead Detection (Phase 3)
# ---------------------------------------------------------------------------

def detect_lost_leads():
    """
    Detect and mark leads as LOST when their pickup date has passed
    and they were never converted.

    Runs once per scheduler cycle (every 30 min). Only touches leads
    whose pickup_date < today and status is still new/contacted/interested/future_contact.

    Also handles:
    - Leads contacted but never replied (past pickup = lost opportunity)
    - Leads that were interested but never booked
    """
    from reservations.models import Lead
    from .models import LeadActivity
    from .services import GoHighLevelService

    today = timezone.now().date()

    # Find leads past their pickup date that aren't already converted/lost/cold
    active_statuses = [
        Lead.StatusChoices.NEW,
        Lead.StatusChoices.CONTACTED,
        Lead.StatusChoices.INTERESTED,
        Lead.StatusChoices.FUTURE_CONTACT,
    ]

    expired_leads = Lead.objects.filter(
        pickup_date__lt=today,
        status__in=active_statuses,
        converted=False,
    ).select_related()

    if not expired_leads.exists():
        return {"lost": 0}

    lost_count = 0
    service = None

    for lead in expired_leads[:200]:  # Cap per cycle
        old_status = lead.status
        lead.status = Lead.StatusChoices.LOST
        lead.save(update_fields=["status"])

        # Cancel any lingering follow-up tasks
        if lead.sequence_active:
            cancel_lead_sequence(lead.id, reason="expired_date")

        # Log the transition
        days_past = (today - lead.pickup_date).days
        LeadActivity.objects.create(
            lead=lead,
            activity_type=LeadActivity.ActivityType.STATUS_CHANGE,
            description=(
                f"Auto-marked LOST: pickup date {lead.pickup_date} was {days_past} day(s) ago. "
                f"Previous status: {old_status}."
            ),
            metadata={
                "old_status": old_status,
                "new_status": "lost",
                "pickup_date": str(lead.pickup_date),
                "days_past_pickup": days_past,
                "had_replied": lead.has_replied,
                "contact_attempts": lead.contact_attempts or 0,
            },
        )

        # Apply lifecycle tags (best-effort)
        if lead.ghl_contact_id:
            try:
                if service is None:
                    service = GoHighLevelService()
                service.apply_lifecycle_tags(lead.ghl_contact_id, lead, "lost")
            except Exception:
                pass

        lost_count += 1

    logger.info(f"Detected {lost_count} lost leads (pickup date passed, not converted)")
    return {"lost": lost_count}
