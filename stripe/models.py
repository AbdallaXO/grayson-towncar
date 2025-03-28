from django.db import models
from django.contrib.auth.models import User
# Create your models here.
class UserPayment(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    stripe_customer_id = models.CharField(max_length=255)
    stripe_checkout_id = models.CharField(max_length=255)
    stripe_product_id = models.CharField(max_length=255)
    product_name = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2)