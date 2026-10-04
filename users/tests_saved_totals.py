"""The saved unpaid/pending numbers on an agent follow the same rules as Pay Now.

Run with:  ./manage.py test users.tests_saved_totals

Those numbers feed the agency pages and reports. They used to count only trips
someone clicked "completed", so a trip that passed its date on its own was owed
(and paid) but missing from what the agency saw.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation
from users.eligibility import refresh_saved_totals
from users.models import TravelAgent


class SavedTotalsTests(TestCase):
    def setUp(self):
        self.vehicle = Vehicle.objects.create(vehicle_type="sedan", capacity=4, luggage_capacity=4)
        self.route = Route.objects.create(origin=Location.objects.create(name="MCO"),
                                          destination=Location.objects.create(name="Resort"))
        self.rate = Rate.objects.create(vehicle=self.vehicle, route=self.route,
                                        oneway_price=Decimal("200"), round_trip_price=Decimal("380"))
        self.customer = Customer.objects.create(first_name="Guest", last_name="One",
                                                email="guest@example.com", phone_number="4075550100")
        user = User.objects.create_user("cassie", email="cassie@example.com")
        self.agent = TravelAgent.objects.create(user=user, agent_name="Cassie", phone="4075550101",
                                                commission_rate=Decimal("10"), payment_method="venmo")

    def book(self, price, *, days_ago, status="confirmed"):
        res = Reservation.objects.create(
            trip_type="one_way", customer=self.customer, rate=self.rate, base_price=Decimal(price),
            total_price=Decimal(price), status=status, travel_agent=self.agent,
        )
        day = timezone.localdate() - timedelta(days=days_ago)
        Leg.objects.create(reservation=res, route=self.route, vehicle=self.vehicle,
                           pickup_date=day, pickup_time=time(10, 0), status="confirmed")
        refresh_saved_totals([self.agent.id])  # legs are added after the booking saves
        return res

    def saved(self):
        self.agent.refresh_from_db()
        return self.agent.unpaid_commissions, self.agent.pending_commissions

    def test_past_trip_nobody_clicked_completed_counts_as_owed(self):
        self.book("200", days_ago=5)                      # past, still "confirmed"
        self.book("300", days_ago=2, status="completed")  # clicked completed
        self.assertEqual(self.saved(), (Decimal("50.00"), Decimal("0.00")))

    def test_future_trip_is_pending(self):
        self.book("200", days_ago=-10)
        self.assertEqual(self.saved(), (Decimal("0.00"), Decimal("20.00")))

    def test_hourly_recount_picks_up_trips_that_passed_their_date(self):
        res = self.book("200", days_ago=-10)
        Leg.objects.filter(reservation=res).update(pickup_date=timezone.localdate() - timedelta(days=3))
        self.assertEqual(self.saved(), (Decimal("0.00"), Decimal("20.00")))  # nothing saved since
        self.assertEqual(refresh_saved_totals(), 1)
        self.assertEqual(self.saved(), (Decimal("20.00"), Decimal("0.00")))
        self.assertEqual(refresh_saved_totals(), 0)  # nothing left to change

    def test_paid_and_excluded_trips_are_not_owed(self):
        paid = self.book("200", days_ago=5)
        personal = self.book("300", days_ago=5)
        Reservation.objects.filter(pk=paid.pk).update(commission_paid=True)
        Reservation.objects.filter(pk=personal.pk).update(commission_excluded=True)
        refresh_saved_totals()
        self.assertEqual(self.saved(), (Decimal("0.00"), Decimal("0.00")))

    def test_deleting_a_booking_recounts(self):
        res = self.book("200", days_ago=5)
        self.assertEqual(self.saved()[0], Decimal("20.00"))
        res.legs.all().delete()
        res.delete()
        self.assertEqual(self.saved()[0], Decimal("0.00"))
