"""Commission check: possible personal trips and duplicate bookings.

Run with:  ./manage.py test users.tests_commission_check
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import AuditLog, Customer, Leg, Reservation
from users.commission_check import duplicate_groups, personal_suspects
from users.eligibility import sum_ready
from users.models import CommissionCheck, TravelAgent


class Fixture(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("disp", password="x", is_staff=True)
        self.client.force_login(self.staff)
        self.url = reverse("commission_check")
        self.vehicle = Vehicle.objects.create(vehicle_type="sedan", capacity=4, luggage_capacity=4)
        self.route = Route.objects.create(origin=Location.objects.create(name="MCO"),
                                          destination=Location.objects.create(name="Resort"))
        self.rate = Rate.objects.create(vehicle=self.vehicle, route=self.route,
                                        oneway_price=Decimal("200"), round_trip_price=Decimal("380"))
        user = User.objects.create_user("cassie", email="cassie@example.com")
        self.agent = TravelAgent.objects.create(user=user, agent_name="Cassie Chin", phone="4075550101",
                                                commission_rate=Decimal("10"), payment_method="venmo")

    def book(self, first, last, *, price="200", days_ago=5, email="guest@example.com", phone="4075550100", notes=""):
        rider = Customer.objects.create(first_name=first, last_name=last, email=email, phone_number=phone)
        res = Reservation.objects.create(
            trip_type="one_way", customer=rider, rate=self.rate, base_price=Decimal(price),
            total_price=Decimal(price), status="confirmed", travel_agent=self.agent, private_notes=notes,
        )
        Leg.objects.create(reservation=res, route=self.route, vehicle=self.vehicle,
                           pickup_date=timezone.localdate() - timedelta(days=days_ago),
                           pickup_time=time(10, 0), status="confirmed",
                           pickup_location="MCO", dropoff_location="Resort")
        return res


class PersonalTests(Fixture):
    def flagged(self):
        return {f.reservation.id: f.reasons for f in personal_suspects()}

    def test_rider_is_the_agent(self):
        res = self.book("Cassie", "Chin")
        self.assertIn("Rider is the agent", self.flagged()[res.id][0])

    def test_same_last_name_and_discount_and_notes(self):
        family = self.book("Tom", "Chin")
        cheap = self.book("Ann", "Lee", price="150")
        noted = self.book("Bo", "Park", notes="Agent rate, personal trip")
        flagged = self.flagged()
        self.assertIn("last name", flagged[family.id][0])
        self.assertIn("25% under the normal rate", flagged[cheap.id][0])
        self.assertTrue(flagged[noted.id][0].startswith("Notes say"))

    def test_agents_own_email_on_a_clients_trip_is_not_flagged(self):
        res = self.book("Ann", "Lee", email="cassie@example.com", phone="4075550101")
        self.assertNotIn(res.id, self.flagged())

    def test_not_commissionable_stops_the_payment_and_is_logged(self):
        res = self.book("Cassie", "Chin")
        self.assertEqual(sum_ready(self.agent), Decimal("20.00"))
        self.client.post(self.url, {"action": "personal_exclude", "reservation": res.id, "tab": "personal"})
        res.refresh_from_db()
        self.assertTrue(res.commission_excluded)
        self.assertEqual(sum_ready(self.agent), Decimal("0.00"))
        self.assertNotIn(res.id, self.flagged())
        self.assertTrue(AuditLog.objects.filter(object_id=res.id, field_name="commission").exists())

    def test_its_fine_keeps_paying_and_never_comes_back(self):
        res = self.book("Cassie", "Chin")
        self.client.post(self.url, {"action": "personal_fine", "reservation": res.id})
        res.refresh_from_db()
        self.assertFalse(res.commission_excluded)
        self.assertEqual(sum_ready(self.agent), Decimal("20.00"))
        self.assertNotIn(res.id, self.flagged())

    def test_already_paid_is_refused(self):
        res = self.book("Cassie", "Chin")
        Reservation.objects.filter(pk=res.pk).update(commission_paid=True)
        response = self.client.post(self.url, {"action": "personal_exclude", "reservation": res.id}, follow=True)
        self.assertContains(response, "already paid")
        res.refresh_from_db()
        self.assertFalse(res.commission_excluded)

    def test_page_lists_them(self):
        res = self.book("Cassie", "Chin")
        response = self.client.get(self.url)
        self.assertContains(response, f"#{res.display_number}")
        self.assertContains(response, "Not commissionable")


class DuplicateTests(Fixture):
    def test_same_rider_same_day_is_a_group(self):
        a = self.book("Ann", "Lee")
        b = self.book("ann", "lee")
        self.book("Ann", "Lee", days_ago=9)  # another day: not a duplicate
        groups = duplicate_groups()
        self.assertEqual([[f.reservation.id for f in g.members] for g in groups], [[a.id, b.id]])

    def test_dont_pay_one_keeps_the_other(self):
        a = self.book("Ann", "Lee")
        b = self.book("Ann", "Lee")
        self.client.post(self.url, {"action": "dup_exclude", "reservation": b.id, "group": [a.id, b.id],
                                    "tab": "duplicates"})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertFalse(a.commission_excluded)
        self.assertTrue(b.commission_excluded)
        self.assertIn(f"#{a.display_number}", b.commission_exclusion_reason)
        self.assertEqual(sum_ready(self.agent), Decimal("20.00"))
        self.assertEqual(duplicate_groups(), [])

    def test_all_real_pays_each_and_never_comes_back(self):
        a = self.book("Ann", "Lee")
        b = self.book("Ann", "Lee")
        self.client.post(self.url, {"action": "dup_fine", "reservation": a.id, "group": [a.id, b.id]})
        self.assertEqual(sum_ready(self.agent), Decimal("40.00"))
        self.assertEqual(duplicate_groups(), [])
        self.assertEqual(CommissionCheck.objects.filter(kind="duplicate", decision="fine").count(), 2)

    def test_non_staff_cannot_open_it(self):
        self.client.force_login(User.objects.create_user("guest", password="x"))
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
