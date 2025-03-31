from django.contrib import admin

# Register your models here.
from .models import Vehicle, Rate, Route
admin.site.register(Vehicle)
admin.site.register(Rate)
admin.site.register(Route)