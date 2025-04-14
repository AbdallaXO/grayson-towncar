from django.contrib import admin
from .models import UserProfile, PartnerForm, ContactUsForm
# Register your models here.

admin.site.register(UserProfile)
admin.site.register(PartnerForm)
admin.site.register(ContactUsForm)
