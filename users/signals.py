from django.dispatch import receiver
import asyncio
from django.db.models.signals import post_save
from . models import PartnerForm

@receiver(post_save,sender=PartnerForm)
def user_form_handler(sender, instance, created, **kwargs):
    if  created:
        form = instance
        name = instance.name
        email = instance.email
        phone_number = instance.phone_number
        print(f'Hello {name} New Blog Post Created')
