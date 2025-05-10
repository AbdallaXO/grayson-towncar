from django.contrib import admin
from .models import (
    UserProfile,
    PartnerForm,
    ContactUsForm,
    NewsLetter,
    TravelAgent,
    CommissionPayout,
)
# Register your models here.

admin.site.register(UserProfile)
admin.site.register(PartnerForm)
admin.site.register(ContactUsForm)
admin.site.register(NewsLetter)
admin.site.register(TravelAgent)
admin.site.register(CommissionPayout)
