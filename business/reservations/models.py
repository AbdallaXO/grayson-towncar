from django.db import models

# Create your models here.
class Reservation(models.Model):
    """
    A simple model for a reservation that has a customer
    """
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, null=True, blank=True)
    pickup_date = models.DateField()
    pickup_time = models.TimeField()
    created = models.DateTimeField(auto_now_add=True)
    pasenger_count = models.PositiveIntegerField()
    luggage_count = models.IntegerField(default=3)
    special_requests = models.TextField(null=True, blank=True)

    def __str__(self):
        return str(self.id)
    


class Customer(models.Model):
    """
    A simple customer model
    """
    name = models.CharField(max_length=100)
    email = models.EmailField(max_length=100)
    zipcode = models.CharField(max_length=10)
    phone_number = models.CharField(max_length=25)

    def __str__(self):
        return self.email

