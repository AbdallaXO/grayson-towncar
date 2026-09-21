"""Schedule board — a job whose flight is cancelled or diverted says so on the chip.

Founder, 2026-09-21: the 4:42 PM arrival sat on the board looking like every
other job while its flight had been cancelled. The tracker had written
"Cancelled" into the flight's status and a task had been filed, but the chip —
the thing the dispatcher actually looks at — said nothing.

What must hold:
  * a chip whose tracked flight status contains "cancel" carries a red
    ✕ CXL badge and a red dashed ring, in a driver's row and in Unassigned;
  * "Diverted" gets an amber DIV badge;
  * every other status (Scheduled, Arrived, Delayed, Not Found…) is untouched;
  * the popup carries a "Flight status" line for the disrupted ones.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import preload_timing_cache
from dispatching.views import _flight_disruption
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Flight, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=5)


class _BoardFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        cls.vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Angeline", last_name="Owens", email="a@example.com", phone_number="5551234567")
        cls.george = Driver.objects.create(
            profile=User.objects.create_user("bfc_george", first_name="George"), driver_type="inhouse")
        DriverVehicleAssignment.objects.create(
            driver=cls.george, date=DAY,
            vehicle=FleetVehicle.objects.create(
                vehicle_number="7", vehicle_type=cls.vehicle, year=2024, make="Chevrolet", model="Suburban"))
        cls.staff = User.objects.create_user("bfc_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _arrival(self, pickup_time=time(16, 42), driver=None, flight_status="Scheduled"):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        flight = Flight.objects.create(
            airline="DL", flight_number="1739", status=flight_status,
            scheduled_arrival_local=datetime.combine(DAY, pickup_time))
        return Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=pickup_time,
            pickup_location="Orlando International Airport (MCO)", dropoff_location="Disney",
            route=self.route, status="confirmed", driver=driver, flight_information=flight)

    def _board(self):
        return self.client.get(reverse("schedule_board") + f"?date={DAY.isoformat()}")

    def _george_slots(self, resp):
        row = next(r for r in resp.context["inhouse_timeline"] if r["driver"].id == self.george.id)
        return list(row["schedule"].slots)


class FlightDisruptionHelperTests(TestCase):
    def test_reads_the_trackers_words(self):
        class _L:
            def __init__(self, status):
                self.flight_information = type("F", (), {"status": status})()
        self.assertEqual(_flight_disruption(_L("Cancelled")), ("cancelled", "Cancelled"))
        self.assertEqual(_flight_disruption(_L("cancelled / gate closed")), ("cancelled", "cancelled / gate closed"))
        self.assertEqual(_flight_disruption(_L("Diverted")), ("diverted", "Diverted"))
        self.assertEqual(_flight_disruption(_L("Arrived / Gate Arrival")), ("", "Arrived / Gate Arrival"))
        self.assertEqual(_flight_disruption(_L("Not Found")), ("", "Not Found"))
        self.assertEqual(_flight_disruption(None), ("", ""))


class FlightCancelledOnTheBoardTests(_BoardFixture):
    def test_a_cancelled_flight_is_shouted_on_the_drivers_chip(self):
        leg = self._arrival(driver=self.george, flight_status="Cancelled")
        resp = self._board()
        slot = self._george_slots(resp)[0]
        self.assertEqual(slot.flight_disruption, "cancelled")
        self.assertEqual(slot.flight_status, "Cancelled")
        html = resp.content.decode()
        self.assertIn("tl-flight-cancelled", html)
        self.assertIn("CXL", html)
        self.assertIn('data-flight-disruption="cancelled"', html)
        self.assertIn("CANCELLED — call the guest", html)

    def test_a_cancelled_flight_is_shouted_in_the_unassigned_row(self):
        self._arrival(flight_status="Cancelled")
        resp = self._board()
        chip = resp.context["unassigned_timeline_slots"][0]
        self.assertEqual(chip["flight_disruption"], "cancelled")
        self.assertIn("tl-flight-cancelled", resp.content.decode())

    def test_a_diverted_flight_is_amber(self):
        self._arrival(driver=self.george, flight_status="Diverted")
        resp = self._board()
        self.assertEqual(self._george_slots(resp)[0].flight_disruption, "diverted")
        html = resp.content.decode()
        self.assertIn("tl-flight-diverted", html)
        self.assertIn(">DIV<", html)
        self.assertNotIn("tl-flight-cancelled", html.split("</style>")[-1])

    def test_an_ordinary_flight_is_left_alone(self):
        self._arrival(driver=self.george, flight_status="Arrived / Delayed")
        resp = self._board()
        self.assertEqual(self._george_slots(resp)[0].flight_disruption, "")
        body = resp.content.decode().split("</style>")[-1]
        self.assertNotIn("tl-flight-cancelled", body)
        self.assertNotIn("tl-flight-flag", body)
