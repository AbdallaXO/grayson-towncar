import logging
from django.db.models.signals import post_save, pre_save, pre_delete
from django.dispatch import receiver
from django.db import transaction
from decimal import Decimal
from .emails import thankyou_email, agent_register_email
from .models import PartnerForm, ContactUsForm, TravelAgent, CommissionPayout, AgencyCommissionPayout
from .utils import create_or_find_travel_agent

logger = logging.getLogger(__name__)

# ======== FORM EMAIL NOTIFICATIONS ========

@receiver(post_save, sender=PartnerForm)
@receiver(post_save, sender=ContactUsForm)
def handle_form_submission(sender, instance, created, **kwargs):
    """Send email confirmation when a form is submitted"""
    if created:
        try:
            logger.info(f"Sending email to {instance} from {sender.__name__}")
            thankyou_email(instance)
        except Exception as e:
            logger.error(f"Error sending email for {sender.__name__}: {e}")


@receiver(post_save, sender=TravelAgent)
def create_hubspot_contact(sender, instance, created, **kwargs):
    """Create a HubSpot contact for new travel agents"""
    if created:
        try:
            logger.info(f"Creating a Hubspot Contact for {instance.agent_name}")
            create_or_find_travel_agent(instance)
        except Exception as e:
            logger.error(f"Error creating an agent contact: {e}")


@receiver(post_save, sender=TravelAgent)
def travel_agent_email(sender, instance, created, **kwargs):
    """Send welcome email to new travel agents"""
    if created:
        try:
            logger.info(f"Sending welcome email to {instance}")
            agent_register_email(instance)
        except Exception as e:
            logger.error(f"Error sending agent email: {e}")


# ======== COMMISSION PAYOUT TRACKING ========

@receiver(pre_save, sender=CommissionPayout)
def track_payout_amount_change(sender, instance, **kwargs):
    """
    Store original amount before save for change detection.
    
    This signal handler runs before a CommissionPayout is saved and:
    1. Ensures payout amounts are never negative
    2. Tracks the previous amount for existing payouts to calculate adjustments
    """
    # Ensure payout amount is never negative
    if instance.total_amount < Decimal("0"):
        logger.warning(
            f"Preventing negative payout amount: ${instance.total_amount}. Setting to 0."
        )
        instance.total_amount = Decimal("0")

    if instance.pk:  # Only for existing payouts
        try:
            # Get the previous state
            old_instance = CommissionPayout.objects.get(pk=instance.pk)

            # Store the old amount as an attribute if it changed
            if old_instance.total_amount != instance.total_amount:
                instance._old_amount = old_instance.total_amount
                logger.info(
                    f"Payout #{instance.pk} amount changing from ${old_instance.total_amount} to ${instance.total_amount}"
                )
        except CommissionPayout.DoesNotExist:
            pass


@receiver(post_save, sender=CommissionPayout)
def handle_payout_changes(sender, instance, created, **kwargs):
    """
    Update agent's total_paid_commission when payouts are created or updated.
    
    This signal handler runs after a CommissionPayout is saved and:
    1. For new payouts - adds the total amount to the agent's total_paid_commission
    2. For updated payouts - adjusts the agent's total_paid_commission by the difference
    """
    with transaction.atomic():
        # Get the agent
        agent = instance.agent
        
        # Determine the amount to adjust
        if created:
            # For new payouts, add the entire amount
            adjustment = instance.total_amount
            logger.info(f"New payout #{instance.id} created for {agent} with amount ${adjustment}")
        elif hasattr(instance, "_old_amount"):
            # For updated payouts, add the difference
            adjustment = instance.total_amount - instance._old_amount
            if adjustment == 0:
                return  # No change in amount
            logger.info(f"Payout #{instance.id} updated for {agent}, adjusting by ${adjustment}")
        else:
            # No adjustment needed
            return
            
        # Update the agent's total_paid_commission with safeguard against negative values
        old_paid = agent.total_paid_commission
        new_paid = max(Decimal("0"), old_paid + adjustment)  # Prevent negative values
        
        if new_paid != old_paid + adjustment:
            logger.warning(
                f"Prevented negative paid commission for {agent}. Calculated: ${old_paid + adjustment}, Set: ${new_paid}"
            )
        
        agent.total_paid_commission = new_paid
        
        logger.info(
            f"Adjusting agent {agent} paid commission from ${old_paid} to ${agent.total_paid_commission}"
        )
        
        # Save agent
        agent.save(update_fields=["total_paid_commission"])


@receiver(pre_delete, sender=CommissionPayout)
def handle_payout_deletion(sender, instance, **kwargs):
    """
    Update agent stats and reservations when a payout is deleted.
    
    This signal handler runs before a CommissionPayout is deleted and:
    1. Marks all related reservations as unpaid
    2. Subtracts the payout amount from the agent's total_paid_commission
    3. Updates the agent's unpaid commissions calculation
    """
    with transaction.atomic():
        # Get the agent
        agent = instance.agent

        logger.info(
            f"Deleting payout #{instance.id} for {agent} with amount ${instance.total_amount}"
        )

        # Mark all reservations in this payout as unpaid
        reservations = instance.reservations.all()
        reservations.update(commission_paid=False, commission_paid_at=None)

        # Update agent's paid commission with safeguard against negative values
        new_paid = max(
            Decimal("0"), agent.total_paid_commission - instance.total_amount
        )

        if new_paid != agent.total_paid_commission - instance.total_amount:
            logger.warning(
                f"Prevented negative paid commission for {agent}. Calculated: ${agent.total_paid_commission - instance.total_amount}, Set: ${new_paid}"
            )

        agent.total_paid_commission = new_paid

        # Update agent's unpaid commission
        agent.update_unpaid_commissions()

        # Save agent
        agent.save(update_fields=["total_paid_commission"])

        logger.info(
            f"Agent after payout deletion - paid_commission: ${agent.total_paid_commission}"
        )

# ======== AGENCY COMMISSION PAYOUT TRACKING ========

@receiver(pre_save, sender=AgencyCommissionPayout)
def track_agency_payout_amount_change(sender, instance, **kwargs):
    """
    Store original amount before save for change detection.
    
    This signal handler runs before an AgencyCommissionPayout is saved and:
    1. Ensures payout amounts are never negative
    2. Tracks the previous amount for existing payouts to calculate adjustments
    """
    # Ensure payout amount is never negative
    if instance.total_amount < Decimal("0"):
        logger.warning(
            f"Preventing negative agency payout amount: ${instance.total_amount}. Setting to 0."
        )
        instance.total_amount = Decimal("0")

    if instance.pk:  # Only for existing payouts
        try:
            # Get the previous state
            old_instance = AgencyCommissionPayout.objects.get(pk=instance.pk)

            # Store the old amount as an attribute if it changed
            if old_instance.total_amount != instance.total_amount:
                instance._old_amount = old_instance.total_amount
                logger.info(
                    f"Agency payout #{instance.pk} amount changing from ${old_instance.total_amount} to ${instance.total_amount}"
                )
        except AgencyCommissionPayout.DoesNotExist:
            pass


@receiver(post_save, sender=AgencyCommissionPayout)
def handle_agency_payout_changes(sender, instance, created, **kwargs):
    """
    Update agency's total_paid_commission when payouts are created or updated.
    
    This signal handler runs after an AgencyCommissionPayout is saved and:
    1. For new payouts - adds the total amount to the agency's total_paid_commission
    2. For updated payouts - adjusts the agency's total_paid_commission by the difference
    """
    with transaction.atomic():
        # Get the agency
        agency = instance.agency
        
        # Determine the amount to adjust
        if created:
            # For new payouts, add the entire amount
            adjustment = instance.total_amount
            logger.info(f"New agency payout #{instance.id} created for {agency} with amount ${adjustment}")
        elif hasattr(instance, "_old_amount"):
            # For updated payouts, add the difference
            adjustment = instance.total_amount - instance._old_amount
            if adjustment == 0:
                return  # No change in amount
            logger.info(f"Agency payout #{instance.id} updated for {agency}, adjusting by ${adjustment}")
        else:
            # No adjustment needed
            return
            
        # Update the agency's total_paid_commission with safeguard against negative values
        old_paid = agency.total_paid_commission
        new_paid = max(Decimal("0"), old_paid + adjustment)  # Prevent negative values
        
        if new_paid != old_paid + adjustment:
            logger.warning(
                f"Prevented negative paid commission for agency {agency}. Calculated: ${old_paid + adjustment}, Set: ${new_paid}"
            )
        
        agency.total_paid_commission = new_paid
        
        logger.info(
            f"Adjusting agency {agency} paid commission from ${old_paid} to ${agency.total_paid_commission}"
        )
        
        # Save agency
        agency.save(update_fields=["total_paid_commission"])


@receiver(pre_delete, sender=AgencyCommissionPayout)
def handle_agency_payout_deletion(sender, instance, **kwargs):
    """
    Update agency stats and agent payouts when an agency payout is deleted.
    
    This signal handler runs before an AgencyCommissionPayout is deleted and:
    1. Marks all related agent payouts as deleted
    2. Subtracts the payout amount from the agency's total_paid_commission
    3. Updates the agency's commission stats
    """
    with transaction.atomic():
        # Get the agency
        agency = instance.agency

        logger.info(
            f"Deleting agency payout #{instance.id} for {agency} with amount ${instance.total_amount}"
        )

        # Get all agent payouts linked to this agency payout
        agent_payouts = instance.agent_payouts.all()

        # Update agency's paid commission with safeguard against negative values
        new_paid = max(
            Decimal("0"), agency.total_paid_commission - instance.total_amount
        )

        if new_paid != agency.total_paid_commission - instance.total_amount:
            logger.warning(
                f"Prevented negative paid commission for agency {agency}. Calculated: ${agency.total_paid_commission - instance.total_amount}, Set: ${new_paid}"
            )

        agency.total_paid_commission = new_paid

        # Update agency's commission stats
        agency.update_commission_stats()

        # Save agency
        agency.save(update_fields=["total_paid_commission"])

        logger.info(
            f"Agency after payout deletion - paid_commission: ${agency.total_paid_commission}"
        )

        # Note: We don't delete the agent payouts here because they might be linked to other agency payouts
        # The agent payout deletion will be handled by its own signal