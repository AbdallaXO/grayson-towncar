from django.contrib import admin
from .models import Reservation, Route, Vehicle

admin.site.register(Reservation)
admin.site.register(Route)
admin.site.register(Vehicle)

# Register your models here.
