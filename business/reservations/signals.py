from django.db.models.signals import pre_save, post_save

def email_confirmation(sender, *)