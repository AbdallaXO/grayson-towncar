from django.contrib import admin
from .models import Reservation, Route, Vehicle, Rate

admin.site.register(Reservation)
admin.site.register(Route)
admin.site.register(Vehicle)
admin.site.register(Rate)

# Register your models here.
