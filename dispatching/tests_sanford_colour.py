"""Sanford (SFB) reads as its own airport on every dispatcher surface.

Run with:  ./manage.py test dispatching.tests_sanford_colour

WHY THIS EXISTS. Sanford is about 40 minutes north of MCO, with its own permit
rules and its own drive times, but on a board of blue arrivals and green returns
an SFB job looked exactly like an MCO one — so it got assigned as though it were
one, and the mistake surfaced only after the car was committed. Sanford legs now
paint cyan, and this file is what stops that colour drifting apart between
the board, the planner and the leg list.

What must hold:
  * ONE DEFINITION: every surface asks ``is_sanford_location``, so a leg that is
    Sanford on the board is Sanford in the planner and the list.
  * EITHER END COUNTS: to Sanford and from Sanford both colour, because what a
    dispatcher needs to see is "this touches the other airport".
  * MCO IS UNTOUCHED: the far more common airport keeps its trip-type colour.
  * THE CITY IS NOT THE AIRPORT: a residential address in Sanford, FL is an
    ordinary job. Colouring those would train dispatchers to ignore the colour.
  * TRIP TYPE SURVIVES: the fill changes, the classification does not — an SFB
    run is still an arrival or a return, and flight tracking must not notice.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.analytics import is_sanford_location
from dispatching.scheduler import preload_timing_cache
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=5)

SFB = "Orlando Sanford International Airport (SFB)"
MCO = "Orlando International Airport (MCO)"
DISNEY = "Disney's Contemporary Resort"


class SanfordDetectorTests(TestCase):
    """The single predicate behind the colour."""

    def test_the_airport_in_the_spellings_dispatchers_actually_type(self):
        for text in (SFB, "SFB", "sfb terminal", "Sanford Airport",
                     "Orlando Sanford Intl", "sanford international"):
            with self.subTest(text=text):
                self.assertTrue(is_sanford_location(text))

    def test_mco_is_never_sanford(self):
        for text in (MCO, "MCO", "mco airport", "Terminal B", "baggage claim"):
            with self.subTest(text=text):
                self.assertFalse(is_sanford_location(text))

    def test_the_city_of_sanford_is_not_the_airport(self):
        """A guest living in Sanford is an ordinary job, not an airport run.

        If these coloured, a big share of cyan bars would carry no airport
        at all and dispatchers would learn to discount the colour — which is the
        one thing it cannot survive.
        """
        for text in ("1200 Oak Ave, Sanford, FL 32771", "Sanford, FL",
                     "Hilton Garden Inn Sanford"):
            with self.subTest(text=text):
                self.assertFalse(is_sanford_location(text))

    def test_blank_locations_are_not_sanford(self):
        for text in ("", None):
            with self.subTest(text=text):
                self.assertFalse(is_sanford_location(text or ""))


class _SanfordFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Jane", last_name="Roe", email="jane@example.com",
            phone_number="5551230000")
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user("sfb_sam", first_name="Sam"),
            driver_type="inhouse")
        fleet = FleetVehicle.objects.create(
            vehicle_number="T-9", vehicle_type=cls.vehicle, year=2024,
            make="Lincoln", model="Continental")
        DriverVehicleAssignment.objects.create(
            driver=cls.driver, date=DAY, vehicle=fleet)
        cls.staff = User.objects.create_user("sfb_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _leg(self, pickup=MCO, dropoff=DISNEY, pickup_time=time(9, 0), driver=None):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=pickup_time,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route,
            status="confirmed", driver=driver)


class LegIsSanfordTests(_SanfordFixture):
    def test_either_end_counts(self):
        """Both directions colour — a dispatcher is stacking Sanford work, and a
        drop-off there costs the same lost hour as a pickup."""
        self.assertTrue(self._leg(pickup=SFB, dropoff=DISNEY).is_sanford())
        self.assertTrue(self._leg(pickup=DISNEY, dropoff=SFB).is_sanford())

    def test_mco_legs_are_not_sanford(self):
        self.assertFalse(self._leg(pickup=MCO, dropoff=DISNEY).is_sanford())
        self.assertFalse(self._leg(pickup=DISNEY, dropoff=MCO).is_sanford())

    def test_a_leg_touching_neither_airport_is_not_sanford(self):
        self.assertFalse(
            self._leg(pickup=DISNEY, dropoff="Universal Orlando").is_sanford())

    def test_trip_type_is_untouched_by_the_colour(self):
        """The fill changes; the classification must not.

        ``get_trip_type`` drives flight tracking, the Publix stop and the
        farm-out rules — a rendering change that quietly moved a leg out of
        'arrival' would stop tracking its inbound flight.
        """
        self.assertEqual(self._leg(pickup=SFB, dropoff=DISNEY).get_trip_type(), "arrival")
        self.assertEqual(self._leg(pickup=DISNEY, dropoff=SFB).get_trip_type(), "return")


class ScheduleSlotSanfordTests(_SanfordFixture):
    """The board reads the slot's cached categories, not the address again."""

    def _slots(self, leg):
        from dispatching.scheduler import build_driver_schedules
        scheds = build_driver_schedules([leg], [self.driver], DAY)
        return scheds[self.driver.id].slots

    def test_slot_agrees_with_the_leg(self):
        sfb = self._leg(pickup=SFB, driver=self.driver)
        self.assertTrue(self._slots(sfb)[0].is_sanford)

    def test_mco_slot_is_not_sanford(self):
        mco = self._leg(pickup=MCO, driver=self.driver)
        self.assertFalse(self._slots(mco)[0].is_sanford)


class BoardRendersSanfordTests(_SanfordFixture):
    """End to end: the class that carries the colour reaches the HTML."""

    def _board(self):
        return self.client.get(
            reverse("schedule_board") + f"?date={DAY.isoformat()}")

    def test_an_assigned_sanford_job_carries_the_sanford_class(self):
        self._leg(pickup=SFB, driver=self.driver)
        html = self._board().content.decode()
        self.assertIn("timeline-slot arrival sanford", html)
        self.assertIn('data-sanford="1"', html)

    def test_an_mco_job_does_not(self):
        self._leg(pickup=MCO, driver=self.driver)
        html = self._board().content.decode()
        self.assertNotIn(" sanford", html.split("<!-- Legend -->")[0])
        self.assertIn('data-sanford="0"', html)

    def test_an_unassigned_sanford_job_is_coloured_in_the_backlog(self):
        """The backlog is where the mix-up happens — a job is dragged out of it
        onto a driver — so the chip has to carry the colour too."""
        self._leg(pickup=SFB, driver=None)
        html = self._board().content.decode()
        self.assertIn("timeline-slot tl-chip arrival sanford", html)

    def test_the_legend_names_sanford(self):
        """A colour nobody can look up is a colour that gets guessed at."""
        self.assertIn("Sanford (SFB)", self._board().content.decode())


class EveryBoardCarriesTheColourTests(_SanfordFixture):
    """The dispatch board and the capacity planner render the same class.

    These three pages were coloured separately, each with its own copy of the
    trip palette, so the cheapest way for this to rot is for one of them to be
    updated and the others not.
    """

    def test_dispatch_board_colours_sanford(self):
        self._leg(pickup=SFB, driver=self.driver)
        resp = self.client.get(
            reverse("dashboard") + f"?date={DAY.isoformat()}")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("timeline-slot arrival sanford", html)
        self.assertIn(".timeline-slot.sanford", html)

    def test_capacity_planner_colours_sanford(self):
        self._leg(pickup=SFB, driver=self.driver)
        resp = self.client.get(
            reverse("capacity_planner") + f"?date={DAY.isoformat()}")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("timeline-slot arrival sanford", html)
        self.assertIn(".timeline-slot.sanford", html)


class LegsListNamesSanfordTests(_SanfordFixture):
    """In a list, colour alone is not enough — the chip says the airport."""

    def test_sfb_chip_appears_for_a_sanford_leg(self):
        self._leg(pickup=SFB, driver=self.driver)
        resp = self.client.get(reverse("legs_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('class="sfb-chip"', resp.content.decode())

    def test_no_chip_for_an_mco_leg(self):
        self._leg(pickup=MCO, driver=self.driver)
        resp = self.client.get(reverse("legs_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('class="sfb-chip"', resp.content.decode())
