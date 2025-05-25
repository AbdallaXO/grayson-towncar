import logging
from django.db.models.signals import post_save, pre_save, pre_delete, post_delete
from django.dispatch import receiver
from django.db import transaction
from decimal import Decimal

# Make sure all necessary models are imported
from .models import (
    PartnerForm,
    ContactUsForm,
    TravelAgent,
    CommissionPayout,
    Agency,
    AgencyCommissionPayout,  # Added Agency and AgencyCommissionPayout
)
from .emails import thankyou_email, agent_register_email
from .utils import create_or_find_travel_agent
# from reservations.models import Reservation # Import if other signals need it

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


# ======== COMMISSION PAYOUT TRACKING (for TravelAgent) ========


@receiver(pre_save, sender=CommissionPayout)
def track_payout_amount_change(sender, instance, **kwargs):
    """
    Store original amount before save for change detection.

    This signal handler runs before a CommissionPayout is saved and:
    1. Ensures payout amounts are never negative
    2. Tracks the previous amount for existing payouts to calculate adjustments
    """
    if instance.total_amount < Decimal("0"):
        logger.warning(
            f"Preventing negative payout amount: ${instance.total_amount}. Setting to 0."
        )
        instance.total_amount = Decimal("0")

    if instance.pk:  # Only for existing payouts
        try:
            old_instance = CommissionPayout.objects.get(pk=instance.pk)
            if old_instance.total_amount != instance.total_amount:
                instance._old_amount = old_instance.total_amount
                logger.info(
                    f"Payout #{instance.pk} amount changing from ${old_instance.total_amount} to ${instance.total_amount}"
                )
        except CommissionPayout.DoesNotExist:
            pass  # New payout


@receiver(post_save, sender=CommissionPayout)
def handle_payout_changes(sender, instance, created, **kwargs):
    """
    Update agent's total_paid_commission when payouts are created or updated.
    """
    with transaction.atomic():
        agent = instance.agent
        adjustment = Decimal("0")

        if created:
            adjustment = instance.total_amount
            logger.info(
                f"New payout #{instance.id} created for {agent} with amount ${adjustment}"
            )
        elif hasattr(instance, "_old_amount"):
            adjustment = instance.total_amount - instance._old_amount
            if adjustment == Decimal("0"):
                # Clean up attribute even if no change
                if hasattr(instance, "_old_amount"):
                    del instance._old_amount
                return  # No change in amount
            logger.info(
                f"Payout #{instance.id} updated for {agent}, adjusting by ${adjustment}"
            )
        else:
            # No adjustment needed (e.g. save without amount change and no _old_amount tracked)
            return

        current_agent_paid = (
            agent.total_paid_commission
            if agent.total_paid_commission is not None
            else Decimal("0")
        )
        new_agent_paid = max(Decimal("0"), current_agent_paid + adjustment)

        if new_agent_paid != current_agent_paid + adjustment:
            logger.warning(
                f"Prevented negative paid commission for {agent}. Calculated: ${current_agent_paid + adjustment}, Set: ${new_agent_paid}"
            )

        agent.total_paid_commission = new_agent_paid
        logger.info(
            f"Adjusting agent {agent} paid commission from ${current_agent_paid} to ${agent.total_paid_commission}"
        )
        agent.save(update_fields=["total_paid_commission"])

        # Clean up temporary attribute
        if hasattr(instance, "_old_amount"):
            del instance._old_amount


@receiver(pre_delete, sender=CommissionPayout)
def handle_payout_deletion(sender, instance, **kwargs):
    """
    Update agent stats and reservations when a payout is deleted.
    """
    with transaction.atomic():
        agent = instance.agent
        logger.info(
            f"Deleting payout #{instance.id} for {agent} with amount ${instance.total_amount}"
        )

        # Mark all reservations in this payout as unpaid
        # Ensure instance.reservations.all() is the correct way to get reservations for this payout
        reservations = instance.reservations.all()
        reservations.update(commission_paid=False, commission_paid_at=None)

        current_agent_paid = (
            agent.total_paid_commission
            if agent.total_paid_commission is not None
            else Decimal("0")
        )
        new_agent_paid = max(Decimal("0"), current_agent_paid - instance.total_amount)

        if new_agent_paid != current_agent_paid - instance.total_amount:
            logger.warning(
                f"Prevented negative paid commission for {agent}. Calculated: ${current_agent_paid - instance.total_amount}, Set: ${new_agent_paid}"
            )
        agent.total_paid_commission = new_agent_paid

        # Save this change first
        agent.save(update_fields=["total_paid_commission"])

        # Then update other commission stats like unpaid commissions
        # This method should handle its own saving if it changes fields.
        if hasattr(agent, "update_unpaid_commissions"):
            agent.update_unpaid_commissions()

        logger.info(
            f"Agent after payout deletion - paid_commission: ${agent.total_paid_commission}"
        )


# ======== AGENCY COMMISSION PAYOUT TRACKING ========


@receiver(pre_save, sender=AgencyCommissionPayout)
def track_agency_payout_amount_change(sender, instance, **kwargs):
    """
    Store original amount before save for change detection for AgencyCommissionPayout.
    """
    if instance.total_amount < Decimal("0"):
        logger.warning(
            f"Preventing negative agency payout amount: ${instance.total_amount}. Setting to 0."
        )
        instance.total_amount = Decimal("0")

    if instance.pk:  # Only for existing payouts
        try:
            old_instance = AgencyCommissionPayout.objects.get(pk=instance.pk)
            if old_instance.total_amount != instance.total_amount:
                # Use a distinct attribute name to avoid clashes if signals are ever combined
                instance._old_agency_payout_amount = old_instance.total_amount
                logger.info(
                    f"AgencyPayout #{instance.pk} amount changing from ${old_instance.total_amount} to ${instance.total_amount}"
                )
        except AgencyCommissionPayout.DoesNotExist:
            pass  # New payout


@receiver(post_save, sender=AgencyCommissionPayout)
def handle_agency_payout_changes(sender, instance, created, **kwargs):
    """
    Update Agency's total_paid_commission when AgencyCommissionPayouts are created or updated.
    """
    with transaction.atomic():
        agency = instance.agency
        adjustment = Decimal("0")

        if created:
            adjustment = instance.total_amount
            logger.info(
                f"New AgencyPayout #{instance.id} created for {agency} with amount ${adjustment}"
            )
        elif hasattr(instance, "_old_agency_payout_amount"):
            adjustment = instance.total_amount - instance._old_agency_payout_amount
            if adjustment == Decimal("0"):
                if hasattr(
                    instance, "_old_agency_payout_amount"
                ):  # Cleanup even if no change
                    del instance._old_agency_payout_amount
                return  # No change in amount
            logger.info(
                f"AgencyPayout #{instance.id} updated for {agency}, adjusting by ${adjustment}"
            )
        else:
            return  # No adjustment needed

        current_agency_paid = (
            agency.total_paid_commission
            if agency.total_paid_commission is not None
            else Decimal("0")
        )
        new_agency_paid = max(Decimal("0"), current_agency_paid + adjustment)

        if new_agency_paid != current_agency_paid + adjustment:
            logger.warning(
                f"Prevented negative total_paid_commission for {agency}. Calculated: ${current_agency_paid + adjustment}, Set: ${new_agency_paid}"
            )

        agency.total_paid_commission = new_agency_paid
        logger.info(
            f"Adjusting {agency} total_paid_commission from ${current_agency_paid} to ${agency.total_paid_commission}"
        )
        agency.save(update_fields=["total_paid_commission"])

        if hasattr(instance, "_old_agency_payout_amount"):
            del instance._old_agency_payout_amount


@receiver(post_delete, sender=AgencyCommissionPayout)
def handle_agency_payout_deletion(sender, instance, **kwargs):
    """
    When an agency payout is deleted, also delete all associated agent payouts
    """
    # Get all agent payouts associated with this agency payout
    agent_payouts = instance.agent_payouts.all()

    # Delete each agent payout
    for agent_payout in agent_payouts:
        agent_payout.delete()
