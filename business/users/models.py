from django.db import models
from django.contrib.auth.models import User
# Create your models here.

class UserProfile(models.Model):
    """
    Extended profile for all users of the system
    """
    user = models.OneToOneField(User, on_delete=models.PROTECT, related_name='profile')
    phone_number = models.CharField(max_length=25)
    is_driver = models.BooleanField(default=False)
    is_travel_agent = models.BooleanField(default=False)

    def __str__(self):
        return self.user.email
