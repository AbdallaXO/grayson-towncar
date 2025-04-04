from django.contrib import admin
from .models import UserPayment

# Register your models here.

@admin.register(UserPayment)
class UserPaymentAdmin(admin.ModelAdmin):
    pass
    
