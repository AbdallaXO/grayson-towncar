from django.db import models
from django.contrib.auth.models import User
from reservations.models import Leg


class Driver(models.Model):
    profile = models.OneToOneField(User, on_delete=models.CASCADE)
    vehicle = models.CharField(null=True, blank=True, max_length=55)
    schedule = models.CharField(max_length=255, null=True, blank=True)
    payment_method = models.CharField(
        max_length=50, default="direct deposit", blank=True
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

    def __str__(self):
        if self.profile.first_name:
            return f"{self.profile.first_name} {self.profile.last_name}"
        return self.profile.username


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
            logger.info(f"Creating payment for driver {driver} with {len(legs)} legs. Total: ${total_amount}")

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
                    logger.info(f"Processing leg ID: {leg.id}, Amount: {leg.driver_pay_amount}")
                    
                    # Create the leg payment record explicitly
                    leg_payment = LegPayment(
                        payment=payment, 
                        leg=leg, 
                        amount=leg.driver_pay_amount or 0
                    )
                    leg_payment.save()
                    
                    logger.info(f"Created LegPayment ID: {leg_payment.id}")
                    
                except Exception as e:
                    logger.error(f"Error creating LegPayment for leg {leg.id}: {e}", exc_info=True)
                    # Re-raise the exception to trigger transaction rollback
                    raise

                # Update leg status directly to avoid triggering signals
                try:
                    Leg.objects.filter(id=leg.id).update(payment_status="paid")
                    logger.info(f"Updated leg {leg.id} to paid status")
                except Exception as e:
                    logger.error(f"Error updating leg {leg.id} status: {e}", exc_info=True)
                    raise

            # Verify all LegPayment records were created
            payment_refresh = cls.objects.get(id=payment.id)
            leg_payment_count = payment_refresh.leg_payments.count()
            
            if leg_payment_count != len(legs):
                logger.error(f"MISMATCH: Expected {len(legs)} leg payments but found {leg_payment_count}")
                # This doesn't need to be raised as an exception - just log it
            else:
                logger.info(f"Successfully created {leg_payment_count} leg payment records")
                
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