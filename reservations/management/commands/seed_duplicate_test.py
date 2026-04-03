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
from django.db.models.signals import post_save, pre_save
from django.utils import timezone

from payment.models import Payment
from rates.models import Rate, Vehicle
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
        # Disconnect all post_save/pre_save signals to prevent confirmation emails
        saved_receivers = {}
        for signal in (post_save, pre_save):
            saved_receivers[id(signal)] = signal.receivers[:]
            signal.receivers = []
            signal.sender_receivers_cache.clear()

        self.stdout.write(self.style.WARNING("Signals disconnected — no emails will be sent."))

        try:
            self._do_seed()
        finally:
            for signal in (post_save, pre_save):
                signal.receivers = saved_receivers[id(signal)]
                signal.sender_receivers_cache.clear()
            self.stdout.write(self.style.WARNING("Signals restored."))

    def _do_seed(self):
        user = User.objects.filter(is_superuser=True).first()
        rate = Rate.objects.first()

        if not user or not rate:
            self.stdout.write(self.style.ERROR("Need at least one superuser and one rate."))
            return

        # Build vehicle lookup by type
        vehicles = {v.vehicle_type: v for v in Vehicle.objects.all()}

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
                "pickup": "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
                "dropoff": "Disney's Grand Floridian Resort & Spa, Magic Kingdom Lane, Lake Buena Vista, FL, USA",
                "trip_type": "round_trip",
                "vehicle": "Suv",
                "pax": 4,
                "bags": 5,
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
                "pickup": "Disney's All-Star Sports Resort, West Buena Vista Drive, Lake Buena Vista, FL, USA",
                "dropoff": "Port Canaveral Terminal 3 Carnival, Christopher Columbus Drive, Port Canaveral, FL, USA",
                "trip_type": "one_way",
                "vehicle": "Suv",
                "pax": 6,
                "bags": 8,
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
                "pickup": "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
                "dropoff": "Disney's Wilderness Lodge, Timberline Drive, Lake Buena Vista, FL, USA",
                "trip_type": "round_trip",
                "vehicle": "Mini_Van",
                "pax": 5,
                "bags": 6,
            },
        ]

        # Card-saved case — should NOT show up on the duplicates page
        card_saved_case = {
            "first": "Sarah",
            "last": "CardTest",
            "email": "sarahcard@dupetest.fake",
            "phone": "(555) 999-0000",
            "pickup_date": tomorrow,
            "paid_time": time(16, 0),
            "unpaid_time": time(16, 0),
            "paid_price": Decimal("275.00"),
            "unpaid_price": Decimal("275.00"),
            "pickup": "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
            "dropoff": "Disney's Port Orleans Resort, Orleans Drive, Lake Buena Vista, FL, USA",
            "trip_type": "round_trip",
            "vehicle": "Suv",
            "pax": 3,
            "bags": 4,
        }

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
                vehicle=vehicles.get(tc.get("vehicle", "").lower()),
                status="confirmed",
                trip_type=tc.get("trip_type", "one_way"),
                base_price=tc["paid_price"],
                total_price=tc["paid_price"],
                passenger_count=tc.get("pax", 2),
                luggage_count=tc.get("bags", 2),
                private_notes=SEED_TAG,
                created_by=user,
            )
            Leg.objects.create(
                reservation=paid_res,
                pickup_date=tc["pickup_date"],
                pickup_time=tc["paid_time"],
                pickup_location=tc.get("pickup", "Test Pickup"),
                dropoff_location=tc.get("dropoff", "Test Dropoff"),
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
                vehicle=vehicles.get(tc.get("vehicle", "").lower()),
                status="confirmed",
                trip_type=tc.get("trip_type", "one_way"),
                base_price=tc["unpaid_price"],
                total_price=tc["unpaid_price"],
                passenger_count=tc.get("pax", 2),
                luggage_count=tc.get("bags", 2),
                private_notes=SEED_TAG,
                created_by=user,
            )
            Leg.objects.create(
                reservation=unpaid_res,
                pickup_date=tc["pickup_date"],
                pickup_time=tc["unpaid_time"],
                pickup_location=tc.get("pickup", "Test Pickup"),
                dropoff_location=tc.get("dropoff", "Test Dropoff"),
                status="unassigned",
            )
            # No payment = unpaid
            created += 1

        # Card-saved case: one paid, one card_saved — should NOT appear as duplicate
        tc = card_saved_case
        customer, _ = Customer.objects.get_or_create(
            email=tc["email"],
            defaults={
                "first_name": tc["first"],
                "last_name": tc["last"],
                "phone_number": tc["phone"],
            },
        )
        for payment_status in ("paid", "card_saved"):
            res = Reservation.objects.create(
                customer=customer,
                rate=rate,
                vehicle=vehicles.get(tc.get("vehicle", "").lower()),
                status="confirmed",
                trip_type=tc.get("trip_type", "one_way"),
                base_price=tc["paid_price"],
                total_price=tc["paid_price"],
                passenger_count=tc.get("pax", 2),
                luggage_count=tc.get("bags", 2),
                private_notes=SEED_TAG,
                created_by=user,
            )
            Leg.objects.create(
                reservation=res,
                pickup_date=tc["pickup_date"],
                pickup_time=tc["paid_time"],
                pickup_location=tc.get("pickup", "Test Pickup"),
                dropoff_location=tc.get("dropoff", "Test Dropoff"),
                status="unassigned",
            )
            Payment.objects.create(
                reservation=res,
                customer=customer,
                amount=tc["paid_price"],
                status=payment_status,
                payment_type="pay_later" if payment_status == "card_saved" else "pay_now",
            )
            created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created} test reservations ({len(test_cases)} duplicate groups + 1 card-saved pair that should NOT appear). "
                f"Visit /dispatching/duplicate-reservations/ to see them."
            )
        )
        self.stdout.write(
            self.style.WARNING("Run 'python manage.py seed_duplicate_test --clear' to remove them.")
        )
