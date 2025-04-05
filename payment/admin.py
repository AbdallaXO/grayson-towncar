from django.contrib import admin
from .models import Payment

# Register your models here.


@admin.register(Payment)
class UserPaymentAdmin(admin.ModelAdmin):
    pass
