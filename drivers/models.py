from django.db import models
from django.contrib.auth.models import User
from reservations.models import Leg


class Driver(models.Model):
    DRIVER_TYPE_CHOICES = [
        ("inhouse", "Inhouse"),
        ("affiliate", "Affiliate/Outhouse"),
    ]
    
    profile = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=25, null=True, blank=True, help_text="Driver's phone number")
    vehicle = models.CharField(null=True, blank=True, max_length=55)
    schedule = models.CharField(max_length=255, null=True, blank=True, help_text="Driver's availability schedule (e.g., 'Mon-Thu: 4AM-4PM, Fri: 6PM-8PM, Sat-Sun: 5AM-5PM')")
    notes = models.TextField(null=True, blank=True, help_text="Internal notes about this driver for dispatchers")
    payment_method = models.CharField(
        max_length=50, default="direct deposit", blank=True
    )
    driver_type = models.CharField(
        max_length=20,
        choices=DRIVER_TYPE_CHOICES,
        default="affiliate",
        help_text="Inhouse drivers work for the company and can drive any vehicle. Affiliates are contractors with specific vehicles."
    )

    def get_unpaid_legs(self):
        """Return all legs that are unpaid regardless of status"""
        return self.legs.filter(payment_status="unpaid")

    def get_total_unpaid_amount(self):
        """Calculate total unpaid amount for this driver"""
        return sum(leg.driver_pay_amount or 0 for leg in self.get_unpaid_legs())

    def get_leg_history(self, start_date=None, end_date=None):
        """
        Get driver's leg history with optional date filtering
        """
        legs = self.legs.all()

        if start_date:
            legs = legs.filter(pickup_date__gte=start_date)
        if end_date:
            legs = legs.filter(pickup_date__lte=end_date)

        return legs.order_by("-pickup_date", "-pickup_time")
    
    def get_phone_number(self):
        """Get driver's phone number"""
        return self.phone_number or "N/A"
    
    def get_upcoming_legs(self, days=7):
        """Get upcoming legs for the next N days"""
        from django.utils import timezone
        from datetime import timedelta
        
        today = timezone.localdate()
        end_date = today + timedelta(days=days)
        
        return self.legs.filter(
            pickup_date__gte=today,
            pickup_date__lte=end_date,
            status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"]
        ).order_by("pickup_date", "pickup_time")
    
    def is_available_today(self):
        """Check if driver has any scheduled trips today"""
        from django.utils import timezone
        
        today = timezone.localdate()
        return self.legs.filter(
            pickup_date=today,
            status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"]
        ).exists()
    
    def get_vehicle_display(self):
        """Get vehicle display - 'Any' for inhouse, specific vehicle for affiliates"""
        if self.driver_type == "inhouse":
            return "Any (Inhouse)"
        return self.vehicle or "Not specified"
    
    def get_schedule_display(self):
        """Format schedule for display in multi-line format"""
        if not self.schedule:
            return None
        
        # Split by comma and format each part on a new line
        # This handles formats like "Mon-Thu: 4AM-4PM, Fri: 6PM-8PM, Sat-Sun: 5AM-5PM"
        schedule_parts = [part.strip() for part in self.schedule.split(',')]
        return '\n'.join(schedule_parts)

    def __str__(self):
        if self.profile.first_name:
            return f"{self.profile.first_name} {self.profile.last_name}"
        return self.profile.username


class FleetVehicle(models.Model):
    vehicle_number = models.CharField(max_length=50, unique=True)
    year = models.PositiveIntegerField()
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["vehicle_number"]

    def __str__(self):
        return f"{self.vehicle_number} - {self.year} {self.make} {self.model}"


class DriverVehicleAssignment(models.Model):
    driver = models.ForeignKey(
        "Driver", on_delete=models.CASCADE, related_name="vehicle_assignments"
    )
    date = models.DateField()
    vehicle = models.ForeignKey(
        "FleetVehicle", on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta:
        unique_together = ("driver", "date")
        ordering = ["date", "driver"]

    def __str__(self):
        return f"{self.driver} - {self.vehicle} ({self.date})"


class DriverPayment(models.Model):
    """
    Tracks batches of payments made to drivers
    """

    driver = models.ForeignKey(
        "Driver", on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateTimeField(auto_now_add=True)
    payment_method = models.CharField(max_length=50, default="direct deposit")
    reference_number = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    # Track who created this payment
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_driver_payments",
    )

    class Meta:
        ordering = ["-payment_date"]

    def __str__(self):
        return f"Payment {self.id} to {self.driver}: ${self.amount}"

    @classmethod
    def create_payment(
        cls,
        driver,
        legs,
        payment_method="direct deposit",
        reference_number="",
        notes="",
        created_by=None,
    ):
        """
        Create a payment for multiple legs at once
        """
        from django.db import transaction
        import logging

        logger = logging.getLogger(__name__)

        with transaction.atomic():
            # Calculate the total amount
            total_amount = sum(leg.driver_pay_amount or 0 for leg in legs)

            # Log payment creation
            logger.info(
                f"Creating payment for driver {driver} with {len(legs)} legs. Total: ${total_amount}"
            )

            # Create the payment
            payment = cls.objects.create(
                driver=driver,
                amount=total_amount,
                payment_method=payment_method,
                reference_number=reference_number,
                notes=notes,
                created_by=created_by,
            )

            logger.info(f"Created payment ID: {payment.id}")

            # Create the leg payment records
            for leg in legs:
                try:
                    # Log leg details before creating the payment
                    logger.info(
                        f"Processing leg ID: {leg.id}, Amount: {leg.driver_pay_amount}"
                    )

                    # Create the leg payment record explicitly
                    leg_payment = LegPayment(
                        payment=payment, leg=leg, amount=leg.driver_pay_amount or 0
                    )
                    leg_payment.save()

                    logger.info(f"Created LegPayment ID: {leg_payment.id}")

                except Exception as e:
                    logger.error(
                        f"Error creating LegPayment for leg {leg.id}: {e}",
                        exc_info=True,
                    )
                    # Re-raise the exception to trigger transaction rollback
                    raise

                # Update leg status directly to avoid triggering signals
                try:
                    Leg.objects.filter(id=leg.id).update(payment_status="paid")
                    logger.info(f"Updated leg {leg.id} to paid status")
                except Exception as e:
                    logger.error(
                        f"Error updating leg {leg.id} status: {e}", exc_info=True
                    )
                    raise

            # Verify all LegPayment records were created
            payment_refresh = cls.objects.get(id=payment.id)
            leg_payment_count = payment_refresh.leg_payments.count()

            if leg_payment_count != len(legs):
                logger.error(
                    f"MISMATCH: Expected {len(legs)} leg payments but found {leg_payment_count}"
                )
                # This doesn't need to be raised as an exception - just log it
            else:
                logger.info(
                    f"Successfully created {leg_payment_count} leg payment records"
                )

            return payment


class LegPayment(models.Model):
    """
    Links a payment to the specific legs that were paid
    """

    payment = models.ForeignKey(
        DriverPayment, on_delete=models.CASCADE, related_name="leg_payments"
    )
    leg = models.ForeignKey(
        Leg, on_delete=models.PROTECT, related_name="payment_records"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ("payment", "leg")

    def __str__(self):
        return f"Payment {self.payment.id} - Leg {self.leg.id}"
