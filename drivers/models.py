from django.db import models
from django.contrib.auth.models import User


# Create your models here.
class Driver(models.Model):
    profile = models.OneToOneField(User, on_delete=models.CASCADE)
    vehicle = models.CharField(null=True, blank=True, max_length=55)

    def __str__(self):
        return self.profile.username
