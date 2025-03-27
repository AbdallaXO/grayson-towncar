from django.db import models
from django.contrib.auth.models import User


class Vehicle(models.Model):
    """Model representing different transportation vehicles and their properties."""
    VEHICLE_TYPES = [
        ("towncar", "Towncar"),
        ("suv", "SUV"),
        ("mini_van", "Mini Van"),
        ("van", "Van"),
    ]
    vehicle_type = models.CharField(max_length=20, choices=VEHICLE_TYPES)
    capacity = models.PositiveIntegerField()
    image = models.ImageField(upload_to="vehicles/", blank=True)
    available = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.get_vehicle_type_display()} (Capacity: {self.capacity})"


class Location(models.Model):
    """Model representing pickup and dropoff locations."""
    LOCATION_CATEGORIES = [
        ("mco", "Orlando International Airport (MCO)"),
        ("sanford", "Sanford International Airport (SFB)"),
        ("disney", "All WDW Disney Property And Parks"),
        ("port", "Port Canaveral"),
        ("universal", "All Universal Studios Properties"),
        ("hotel", "Hotel Transfer"),
        ("custom", "Custom Location"),
    ]
    category = models.CharField(max_length=20, choices=LOCATION_CATEGORIES)
    location = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.get_category_display()} - {self.location}"


class Route(models.Model):
    """Model representing a transportation route between two locations."""
    pickup_location = models.ForeignKey(
        Location, related_name="pickup_routes", on_delete=models.CASCADE
    )
    dropoff_location = models.ForeignKey(
        Location, related_name="dropoff_routes", on_delete=models.CASCADE
    )

    class Meta:
        unique_together = ("pickup_location", "dropoff_location")

    def __str__(self):
        return f"{self.pickup_location} - {self.dropoff_location}"


class Rate(models.Model):
    """Model for pricing different vehicle types on specific routes."""
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE)
    route = models.ForeignKey(Route, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ("vehicle", "route")

    def __str__(self):
        return f"{self.vehicle} for {self.route}: ${self.price}"


class Driver(models.Model):
    """Model representing transportation drivers."""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=20)
    license_number = models.CharField(max_length=50)
    available = models.BooleanField(default=True)
    
    def __str__(self):
        return f"{self.user.get_full_name()}"


class Reservation(models.Model):
    """Model for customer transportation reservations."""
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("CONFIRMED", "Confirmed"),
        ("IN_PROGRESS", "In Progress"),
        ("COMPLETED", "Completed"),
        ("CANCELLED", "Cancelled"),
    ]
    
    CARSEAT_TYPES = [
        ("booster", "Booster Seat"),
        ("rf", "Rear Facing Carseat"),
        ("ff", "Forward Facing Carseat"),
    ]
    
    # User and Guest Info
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    guest_name = models.CharField(max_length=100)
    guest_email = models.EmailField()
    guest_phone = models.CharField(max_length=20)

    # Reservation Details
    pickup_date = models.DateField()
    pickup_time = models.TimeField()
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)
    vehicle_type = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True)
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True)

    # Passenger Details
    passenger_count = models.PositiveIntegerField()
    children_count = models.PositiveIntegerField(default=0, blank=True)  # Fixed from PositiveBigIntegerField
    carseat_needed = models.BooleanField(default=False)
    carseat_type = models.CharField(
        max_length=20,
        choices=CARSEAT_TYPES,
        null=True,
        blank=True,
    )
    
    # Trip Details
    luggage_count = models.PositiveIntegerField(default=0)
    airline = models.CharField(max_length=20, null=True, blank=True)
    flight_number = models.CharField(max_length=20, blank=True)
    special_requests = models.TextField(blank=True)
    
    # Status and Timestamps
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
   
    # Payment Info
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    additional_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    payment_status = models.CharField(max_length=20, default='PENDING')
    stripe_payment_id = models.CharField(max_length=100, blank=True)
    
    def __str__(self):
        return f"Reservation - {self.guest_name}"
    
    def save(self, *args, **kwargs):
        # Calculate total price
        self.total_price = self.base_price + self.additional_charges
        super().save(*args, **kwargs)


class Payment(models.Model):
    """Model for tracking payments associated with reservations."""
    reservation = models.OneToOneField(Reservation, on_delete=models.CASCADE)
    stripe_payment_id = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20)
    payment_method = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Payment for {self.reservation}"
    

class SavedCard(models.Model):
    """Model for storing customer payment method information."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    stripe_customer_id = models.CharField(max_length=100)
    stripe_payment_method_id = models.CharField(max_length=100)
    card_last4 = models.CharField(max_length=4)
    card_brand = models.CharField(max_length=20)
    card_exp_month = models.IntegerField()
    card_exp_year = models.IntegerField()
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.card_brand} ending in {self.card_last4}"