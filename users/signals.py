from django.dispatch import receiver
from django.db.models.signals import post_save
from . models import PartnerForm
from . emails import send_partner_confirmation
import logging
logger = logging.getLogger(__name__)

@receiver(post_save, sender=PartnerForm)
def contact_us_partner(sender, instance, created , **kwargs):
    if created:
        print('Created')
        print('Created')
        print('Created')
        print('Created')
        partner = instance
        send_partner_confirmation(partner=partner)
        