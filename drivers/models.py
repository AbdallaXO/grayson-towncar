from django.db import models
from django.contrib.auth.models import User


# Create your models here.
class Driver(models.Model):
    profile = models.OneToOneField(User, on_delete=models.CASCADE)
    legs = models.ManyToManyField("reservations.Leg", blank=True, null=True)

    def __str__(self):
        return self.profile.username
