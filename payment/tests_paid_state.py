"""A reservation's paid flag is owned by its payments, not by whoever saves it.

Found 2026-09-21 while tracing a guest (Mary Tomasso, reservation 18593) who
had paid and still showed as unpaid. The Stripe webhook saves the payment —
the Payment signal sets is_paid=True with an UPDATE — and then saves the
reservation instance it loaded before the payment, writing is_paid=False back.
In production, 2,406 of the 2,415 reservations paid since 1 September 2026 were
flipped back within ten seconds of paying. The pre_save hook here recomputes
the paid-state columns from the payments before any reservation row is
written, so a stale instance can no longer clobber them.
"""
from decimal import Decimal

from django.test import TestCase

from payment.models import Payment
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Reservation


class PaidStateSurvivesStaleSaveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(vehicle_type="towncar", capacity=4, luggage_capacity=4)
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle,
            route=Route.objects.create(
                origin=Location.objects.create(name="A"), destination=Location.objects.create(name="B")),
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Mary", last_name="Tomasso", email="mary@example.com", phone_number="7327403095")

    def _reservation(self):
        return Reservation.objects.create(
            customer=self.customer, rate=self.rate, vehicle=self.vehicle, trip_type="one_way",
            base_price=Decimal("500.00"), total_price=Decimal("500.00"), status="confirmed")

    def test_the_webhook_shape_no_longer_flips_the_flag_back(self):
        reservation = self._reservation()          # the instance the webhook holds
        self.assertFalse(reservation.is_paid)

        payment = Payment(reservation=reservation, customer=self.customer, amount=Decimal("500.00"),
                          status="paid", payment_type="pay_now")
        payment.save()
        # The payment signal has already set the truth in the database...
        self.assertTrue(Reservation.objects.get(pk=reservation.pk).is_paid)

        # ...and the webhook now saves its stale instance, exactly as it does.
        reservation.status = "confirmed"
        reservation.save()

        fresh = Reservation.objects.get(pk=reservation.pk)
        self.assertTrue(fresh.is_paid)
        self.assertEqual(fresh.paid_amount, Decimal("500.00"))
        self.assertEqual(fresh.gross_paid, Decimal("500.00"))
        self.assertIsNotNone(fresh.first_paid_at)
        # The in-memory instance was corrected too, so later reads agree.
        self.assertTrue(reservation.is_paid)

    def test_a_save_that_names_other_fields_is_left_alone(self):
        reservation = self._reservation()
        Payment.objects.create(reservation=reservation, customer=self.customer,
                               amount=Decimal("500.00"), status="paid", payment_type="pay_now")
        stale = Reservation.objects.get(pk=reservation.pk)
        Reservation.objects.filter(pk=reservation.pk).update(is_paid=False)  # simulate drift
        stale.status = "confirmed"
        stale.save(update_fields=["status"])
        # Nothing about the flag was written either way; the backfill fixes drift.
        self.assertFalse(Reservation.objects.get(pk=reservation.pk).is_paid)

    def test_a_refund_is_reflected_on_a_later_full_save(self):
        reservation = self._reservation()
        payment = Payment.objects.create(reservation=reservation, customer=self.customer,
                                         amount=Decimal("500.00"), status="paid", payment_type="pay_now")
        Payment.objects.filter(pk=payment.pk).update(refunded_amount=Decimal("500.00"))  # bypasses signals
        reservation.save()  # any full save recomputes from the rows
        fresh = Reservation.objects.get(pk=reservation.pk)
        self.assertFalse(fresh.is_paid)
        self.assertEqual(fresh.total_refunded, Decimal("500.00"))
