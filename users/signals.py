import logging
from django.db.models.signals import post_save, pre_save, pre_delete
from django.dispatch import receiver
from django.db import transaction
from decimal import Decimal
from .emails import thankyou_email, agent_register_email
from .models import PartnerForm, ContactUsForm, TravelAgent, CommissionPayout

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
    """Store original amount before save for change detection"""
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
def handle_payout_amount_change(sender, instance, created, **kwargs):
    """Update agent stats when payout amount changes"""
    # Skip for new payouts and if no change detected
    if created or not hasattr(instance, "_old_amount"):
        return

    with transaction.atomic():
        # Calculate the difference
        difference = instance.total_amount - instance._old_amount

        # Get the agent
        agent = instance.agent

        # Update the agent's total_paid_commission with safeguard against negative values
        old_paid = agent.total_paid_commission
        new_paid = max(Decimal("0"), old_paid + difference)  # Prevent negative values

        if new_paid != old_paid + difference:
            logger.warning(
                f"Prevented negative paid commission for {agent}. Calculated: ${old_paid + difference}, Set: ${new_paid}"
            )

        agent.total_paid_commission = new_paid

        logger.info(
            f"Adjusting agent {agent} paid commission from ${old_paid} to ${agent.total_paid_commission}"
        )

        # Save agent
        agent.save(update_fields=["total_paid_commission"])


@receiver(pre_delete, sender=CommissionPayout)
def handle_payout_deletion(sender, instance, **kwargs):
    """Update agent stats and reservations when a payout is deleted"""
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
