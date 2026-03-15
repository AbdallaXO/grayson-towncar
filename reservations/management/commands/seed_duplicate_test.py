"""
Create test duplicate reservations to verify the Duplicates page.

Usage:
    python manage.py seed_duplicate_test          # create test data
    python manage.py seed_duplicate_test --clear  # remove test data
"""

from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from payment.models import Payment
from rates.models import Rate
from reservations.models import Customer, Leg, Reservation

SEED_TAG = "[DUPE_TEST]"


class Command(BaseCommand):
    help = "Seed test duplicate reservations for the Duplicates page"

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Remove all seeded test data",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()
            return

        self._seed()

    def _clear(self):
        reservations = Reservation.objects.filter(private_notes__contains=SEED_TAG)
        count = reservations.count()
        if count == 0:
            self.stdout.write(self.style.WARNING("No test duplicates found."))
            return

        # Delete payments first (PROTECT FK), then reservations (cascades legs)
        for res in reservations:
            res.payments.all().delete()
        reservations.delete()

        # Clean up test customers
        Customer.objects.filter(email__endswith="@dupetest.fake").delete()

        self.stdout.write(self.style.SUCCESS(f"Cleared {count} test reservations."))

    def _seed(self):
        user = User.objects.filter(is_superuser=True).first()
        rate = Rate.objects.first()

        if not user or not rate:
            self.stdout.write(self.style.ERROR("Need at least one superuser and one rate."))
            return

        today = timezone.now().date()
        tomorrow = today + timedelta(days=1)

        test_cases = [
            {
                "first": "John",
                "last": "Doe",
                "email": "johndoe@dupetest.fake",
                "phone": "(555) 123-4567",
                "pickup_date": tomorrow,
                "paid_time": time(10, 0),
                "unpaid_time": time(10, 30),  # slightly different time
                "paid_price": Decimal("150.00"),
                "unpaid_price": Decimal("150.00"),
            },
            {
                "first": "Jane",
                "last": "Smith",
                "email": "janesmith@dupetest.fake",
                "phone": "555.987.6543",
                "pickup_date": tomorrow,
                "paid_time": time(14, 0),
                "unpaid_time": time(14, 0),  # exact same time
                "paid_price": Decimal("225.00"),
                "unpaid_price": Decimal("225.00"),
            },
            {
                "first": "Bob",
                "last": "Johnson",
                "email": "bjohnson@dupetest.fake",
                "phone": "5551112222",
                "pickup_date": today + timedelta(days=3),
                "paid_time": time(8, 0),
                "unpaid_time": time(9, 0),  # different time, same day
                "paid_price": Decimal("300.00"),
                "unpaid_price": Decimal("175.00"),  # different price too
            },
        ]

        created = 0
        for tc in test_cases:
            customer, _ = Customer.objects.get_or_create(
                email=tc["email"],
                defaults={
                    "first_name": tc["first"],
                    "last_name": tc["last"],
                    "phone_number": tc["phone"],
                },
            )

            # Create PAID reservation
            paid_res = Reservation.objects.create(
                customer=customer,
                rate=rate,
                status="confirmed",
                base_price=tc["paid_price"],
                total_price=tc["paid_price"],
                private_notes=SEED_TAG,
                created_by=user,
            )
            Leg.objects.create(
                reservation=paid_res,
                pickup_date=tc["pickup_date"],
                pickup_time=tc["paid_time"],
                pickup_location="Test Pickup",
                dropoff_location="Test Dropoff",
                status="unassigned",
            )
            Payment.objects.create(
                reservation=paid_res,
                customer=customer,
                amount=tc["paid_price"],
                status="paid",
                payment_type="pay_now",
            )
            created += 1

            # Create UNPAID duplicate
            unpaid_res = Reservation.objects.create(
                customer=customer,
                rate=rate,
                status="confirmed",
                base_price=tc["unpaid_price"],
                total_price=tc["unpaid_price"],
                private_notes=SEED_TAG,
                created_by=user,
            )
            Leg.objects.create(
                reservation=unpaid_res,
                pickup_date=tc["pickup_date"],
                pickup_time=tc["unpaid_time"],
                pickup_location="Test Pickup",
                dropoff_location="Test Dropoff",
                status="unassigned",
            )
            # No payment = unpaid
            created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created} test reservations ({created // 2} duplicate groups). "
                f"Visit /dispatching/duplicate-reservations/ to see them."
            )
        )
        self.stdout.write(
            self.style.WARNING("Run 'python manage.py seed_duplicate_test --clear' to remove them.")
        )
