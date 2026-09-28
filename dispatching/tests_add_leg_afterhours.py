"""Tests that adding a leg to an existing booking prices the after-hours fee.

Run with:  ./manage.py test dispatching.tests_add_leg_afterhours

add_leg_to_reservation created the leg and touched neither home of the $20
after-hours fee: not the per-leg marker (so a later flight-delay pass asked for
money nobody had been charged) and not the reservation's charges (so 23 late legs
were driven without the fee ever reaching a bill).
"""
import json
from datetime import date, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.test import TestCase
from django.urls import reverse

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Reservation
from reservations.signals import reservation_saved
from reservations.utils import AFTERHOURS_FEE_AMOUNT

LATE = "23:30"
DAYTIME = "14:00"


class AddLegAfterHoursFeeTests(TestCase):
    def setUp(self):
        post_save.disconnect(reservation_saved, sender=Reservation)
        self.addCleanup(
            lambda: post_save.connect(reservation_saved, sender=Reservation)
        )

        self.staff = User.objects.create_user(
            username="dispatcher", password="pw", is_staff=True
        )
        self.client.force_login(self.staff)

        vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"),
        )
        rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("195.00"), round_trip_price=Decimal("350.00"),
        )
        customer = Customer.objects.create(
            first_name="Kate", last_name="Zabel",
            email="kate@example.com", phone_number="4075550142",
        )
        self.reservation = Reservation.objects.create(
            trip_type="one-way", customer=customer, rate=rate, vehicle=vehicle,
            base_price=Decimal("195.00"), total_price=Decimal("195.00"),
            additional_charges=Decimal("0.00"), status="confirmed",
        )

    def _add_leg(self, pickup_time):
        return self.client.post(
            reverse("add_leg_to_reservation"),
            data=json.dumps({
                "reservation_id": str(self.reservation.uuid),
                "leg_data": {
                    "pickup_date": "2026-11-04",
                    "pickup_time": pickup_time,
                    "pickup_location": "MCO",
                    "dropoff_location": "Disney",
                },
            }),
            content_type="application/json",
        )

    def test_late_leg_is_billed_and_marked(self):
        """The fee has to land in BOTH homes or the trip is wrong one way."""
        response = self._add_leg(LATE)
        payload = response.json()
        self.assertTrue(payload["success"], payload)

        self.reservation.refresh_from_db()
        leg = self.reservation.legs.get(id=payload["leg"]["id"])

        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(self.reservation.additional_charges, AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(self.reservation.total_price, Decimal("215.00"))

    def test_late_leg_reports_the_fee_it_added(self):
        payload = self._add_leg(LATE).json()
        self.assertEqual(Decimal(payload["afterhours_fee_added"]), AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(Decimal(payload["reservation_total_price"]), Decimal("215.00"))

    def test_late_leg_is_not_flagged_as_owing(self):
        """The regression this closes: charged once, then asked for again."""
        payload = self._add_leg(LATE).json()
        leg = self.reservation.legs.get(id=payload["leg"]["id"])
        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_daytime_leg_is_untouched(self):
        payload = self._add_leg(DAYTIME).json()
        self.reservation.refresh_from_db()
        leg = self.reservation.legs.get(id=payload["leg"]["id"])

        self.assertEqual(leg.afterhours_fee or Decimal("0.00"), Decimal("0.00"))
        self.assertEqual(self.reservation.additional_charges, Decimal("0.00"))
        self.assertEqual(self.reservation.total_price, Decimal("195.00"))
        self.assertEqual(Decimal(payload["afterhours_fee_added"]), Decimal("0.00"))

    def test_two_late_legs_bill_the_fee_twice(self):
        """One $20 per late leg, matching how the booking wizard prices them."""
        self._add_leg(LATE)
        self._add_leg(LATE)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.additional_charges, Decimal("40.00"))
        self.assertEqual(self.reservation.total_price, Decimal("235.00"))
