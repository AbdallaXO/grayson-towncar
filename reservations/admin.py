from django.contrib import admin
from .models import Reservation, Route, Location, Rate, Vehicle, Customer

admin.site.register(Reservation)
admin.site.register(Route)
admin.site.register(Location)
admin.site.register(Rate)
admin.site.register(Vehicle)
admin.site.register(Customer)
# Register your models here.
