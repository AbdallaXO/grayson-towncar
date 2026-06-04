"""Tests for the optional Vehicle Type Preference in the Schedule Builder.

The control lives in the Schedule Builder modal and flows into build_smart_schedule:
  * 'only'  -> hard-filters this driver's build to the selected vehicle type(s)
  * prefer/heavy -> nudge ordering only (scoring algorithm unchanged)

Run with:  ./manage.py test dispatching.tests_vehicle_preference
"""
from datetime import date, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg
from drivers.models import Driver, FleetVehicle, DriverVehicleAssignment
from dispatching.scheduler import build_smart_schedule

TD = date(2026, 6, 1)


class ScheduleBuilderVehiclePrefTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=4)
        cls.van = Vehicle.objects.create(vehicle_type="van", capacity=10, luggage_capacity=8)
        cls.spr = Vehicle.objects.create(vehicle_type="Van(14 Pax)", capacity=14, luggage_capacity=10)
        o = Location.objects.create(name="MCO")
        d = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(origin=o, destination=d, inhouse_base_pay=Decimal("50.00"))
        cls.rate_suv = Rate.objects.create(
            vehicle=cls.suv, route=cls.route, oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.rate_van = Rate.objects.create(
            vehicle=cls.van, route=cls.route, oneway_price=Decimal("120.00"), round_trip_price=Decimal("200.00"))
        cls.cust = Customer.objects.create(
            first_name="A", last_name="B", email="vp@example.com", phone_number="5550001111")
        u = User.objects.create_user(username="vp_drv", first_name="VP")
        cls.driver = Driver.objects.create(profile=u, driver_type="inhouse")
        # Top-tier vehicle so build_smart_schedule proceeds (compatible with every leg type).
        fv = FleetVehicle.objects.create(
            vehicle_number="900", vehicle_type=cls.spr, year=2022, make="M", model="Sprinter")
        DriverVehicleAssignment.objects.create(driver=cls.driver, date=TD, vehicle=fv)

    def _leg(self, vehicle, t):
        rate = self.rate_suv if vehicle is self.suv else self.rate_van
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.cust, vehicle=vehicle, rate=rate,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=TD, pickup_time=t,
            pickup_location="MCO", dropoff_location="Disney", route=self.route, status="confirmed")

    def _build(self, legs, **kw):
        return build_smart_schedule(
            self.driver.id, str(self.driver), legs, TD, start_hour=0, end_hour=23, **kw)

    def test_only_mode_excludes_other_vehicle_types(self):
        suv1, suv2 = self._leg(self.suv, time(8, 0)), self._leg(self.suv, time(11, 0))
        van1 = self._leg(self.van, time(14, 0))
        legs = [suv1, suv2, van1]
        r = self._build(legs, vehicle_pref_mode="only", preferred_vehicle_types=["suv"])
        scheduled = {s.leg_id for s in r["schedule"]}
        self.assertNotIn(van1.id, scheduled, "van leg must be excluded under only-suv")
        self.assertTrue(scheduled & {suv1.id, suv2.id}, "at least one suv leg should be scheduled")

    def test_only_multiple_types_keeps_listed_excludes_rest(self):
        suv1 = self._leg(self.suv, time(8, 0))
        van1 = self._leg(self.van, time(14, 0))
        legs = [suv1, van1]
        r = self._build(legs, vehicle_pref_mode="only", preferred_vehicle_types=["van", "Van(14 Pax)"])
        scheduled = {s.leg_id for s in r["schedule"]}
        self.assertNotIn(suv1.id, scheduled, "suv leg must be excluded under only-van/14pax")
        self.assertIn(van1.id, scheduled, "van leg should remain")

    def test_no_preference_allows_other_types(self):
        suv1 = self._leg(self.suv, time(8, 0))
        van1 = self._leg(self.van, time(14, 0))
        legs = [suv1, van1]
        r = self._build(legs)
        scheduled = {s.leg_id for s in r["schedule"]}
        self.assertIn(van1.id, scheduled, "with no vehicle preference the van leg is allowed")
