"""Swap Tester fixes (2026-09-28).

  * Vehicle class is the LEG's, not the booking's. A reservation can be an SUV out
    and a Van back; the swap search read only the reservation, called the Van leg an
    SUV job, and offered it to an SUV driver (live: leg 32975, "Give the 9:28 run to
    Angel", Angel in an SUV). The write endpoints now refuse that too.
  * Affiliate takeback lists only pickups still ahead — at 4 PM the 8 AM job is gone.
  * "Take Back → driver" goes through the swap search's own gate (direct_fit), so a
    driver whose shared car is with the partner is never the pick.

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_swap_tester
"""
import json
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import DriverDaySchedule, preload_timing_cache
from dispatching.tests_swap_guards import fake_leg
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = date(2026, 5, 1)
FLEX = {"start": 0, "end": 24, "max_hours": None, "flexible": True}


def _res(vtype):
    return SimpleNamespace(vehicle=SimpleNamespace(vehicle_type=vtype),
                           customer=None, store_stop=False)


class LegVehicleClassTests(TestCase):
    """The swap search judges the class the LEG needs."""

    SUV_DRIVER, VAN_DRIVER = 10, 20

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()

    def test_leg_vehicle_wins_over_the_booking(self):
        from dispatching.swap_optimizer import _get_leg_vtype
        leg = SimpleNamespace(effective_vehicle_type="van", reservation=_res("suv"))
        self.assertEqual(_get_leg_vtype(leg), "van")

    def test_falls_back_to_the_booking_without_a_leg_vehicle(self):
        from dispatching.swap_optimizer import _get_leg_vtype
        self.assertEqual(_get_leg_vtype(SimpleNamespace(reservation=_res("suv"))), "suv")
        self.assertIsNone(_get_leg_vtype(SimpleNamespace(reservation=None)))

    def test_recovery_advisor_reads_the_same_class(self):
        from dispatching.conflict_advisor import _leg_vtype_of
        leg = SimpleNamespace(effective_vehicle_type="van", reservation=_res("suv"))
        self.assertEqual(_leg_vtype_of(leg), "van")

    def _search(self, driver_vtypes):
        from dispatching.swap_optimizer import find_swaps
        target = fake_leg(leg_id=1, pickup=time(9, 28), trip="return")
        target.reservation = _res("suv")      # booked SUV …
        target.effective_vehicle_type = "van"  # … but this leg is the Van
        target.driver_id = None
        target.revenue_share = 0
        schedules = {did: DriverDaySchedule(driver_id=did, driver_name=f"d{did}",
                                            driver_type="inhouse", slots=[])
                     for did in driver_vtypes}
        return find_swaps(target_leg=target, inhouse_schedules=schedules,
                          all_legs_by_id={}, driver_vtypes=driver_vtypes,
                          target_date=DAY,
                          driver_windows={did: FLEX for did in driver_vtypes})

    def test_suv_driver_is_never_offered_a_van_leg(self):
        res = self._search({self.SUV_DRIVER: "suv"})
        self.assertEqual(res.solutions, [])
        self.assertEqual([d.skipped_reason for d in res.diagnostic], ["vehicle_incompatible"])

    def test_van_driver_still_is(self):
        res = self._search({self.SUV_DRIVER: "suv", self.VAN_DRIVER: "van"})
        self.assertTrue(res.solutions)
        self.assertEqual({s.target_driver_id for s in res.solutions}, {self.VAN_DRIVER})


class _BoardFixture(TestCase):
    """An SUV booking whose leg rides in a Van, an SUV driver and a Van driver."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.van = Vehicle.objects.create(vehicle_type="van", capacity=10, luggage_capacity=10)
        route = Route.objects.create(origin=Location.objects.create(name="MCO"),
                                     destination=Location.objects.create(name="Disney"),
                                     inhouse_base_pay=Decimal("50.00"))
        cls.route = route
        cls.rate = Rate.objects.create(vehicle=cls.suv, route=route,
                                       oneway_price=Decimal("100.00"),
                                       round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(first_name="Jane", last_name="Roe",
                                               email="jane@example.com",
                                               phone_number="5551234567")
        cls.angel = cls._driver("st_angel", "Angel", "inhouse", cls.suv, "S-1")
        cls.vic = cls._driver("st_vic", "Vic", "inhouse", cls.van, "V-1")
        cls.affiliate = cls._driver("st_aff", "Oualid", "affiliate")
        cls.staff = User.objects.create_user("st_staff", password="x", is_staff=True)

    @classmethod
    def _driver(cls, username, first, kind, vehicle=None, unit=None):
        drv = Driver.objects.create(
            profile=User.objects.create_user(username, first_name=first), driver_type=kind)
        if vehicle is not None:
            fleet = FleetVehicle.objects.create(vehicle_number=unit, vehicle_type=vehicle,
                                                year=2024, make="Make", model="Model")
            DriverVehicleAssignment.objects.create(driver=drv, date=DAY, vehicle=fleet)
        return drv

    def _leg(self, pickup_time, driver=None, leg_vehicle=None):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.suv, base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver, vehicle=leg_vehicle)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

    @staticmethod
    def _at(hh):
        """The clock at hh:00 on DAY (a with-block), for the rules about now."""
        return patch("django.utils.timezone.now",
                     return_value=timezone.make_aware(datetime.combine(DAY, time(hh, 0))))

    def _post(self, name, payload):
        return self.client.post(reverse(name), data=json.dumps(payload),
                                content_type="application/json")


class VehicleGateOnWriteTests(_BoardFixture):
    """execute_swap / execute_takeback refuse a car that can't serve the leg."""

    def setUp(self):
        super().setUp()
        clock = self._at(6)          # before the day's pickups
        clock.start()
        self.addCleanup(clock.stop)

    def test_swap_refuses_suv_driver_on_van_leg(self):
        leg = self._leg(time(9, 28), leg_vehicle=self.van)
        r = self._post("execute_swap", {"date": DAY.isoformat(),
                                        "moves": [{"leg_id": leg.id,
                                                   "to_driver_id": self.angel.id}]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("needs a Van", r.json()["error"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_swap_lets_van_driver_take_it(self):
        leg = self._leg(time(9, 28), leg_vehicle=self.van)
        with patch("dispatching.views._revalidate_swap_feasibility", return_value=(True, "")):
            r = self._post("execute_swap", {"date": DAY.isoformat(),
                                            "moves": [{"leg_id": leg.id,
                                                       "to_driver_id": self.vic.id}]})
        self.assertEqual(r.status_code, 200, r.content)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.vic.id)

    def test_takeback_refuses_suv_driver_on_van_leg(self):
        leg = self._leg(time(9, 28), driver=self.affiliate, leg_vehicle=self.van)
        r = self._post("execute_takeback", {"date": DAY.isoformat(), "leg_id": leg.id,
                                            "driver_id": self.angel.id})
        self.assertEqual(r.status_code, 409)
        self.assertIn("needs a Van", r.json()["error"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.affiliate.id)


class TakebackListsOnlyUpcomingTests(_BoardFixture):
    """At 4 PM the 8 AM affiliate job can't be taken back — it isn't listed."""

    def _page_at(self, hh):
        now = timezone.make_aware(datetime.combine(DAY, time(hh, 0)))
        with patch("django.utils.timezone.now", return_value=now):
            r = self.client.get(reverse("swap_tester"), {"date": DAY.isoformat()})
        self.assertEqual(r.status_code, 200)
        return r.context

    def test_past_pickups_are_left_out(self):
        morning = self._leg(time(8, 0), driver=self.affiliate)
        evening = self._leg(time(18, 0), driver=self.affiliate)
        ctx = self._page_at(16)
        self.assertEqual([l["id"] for l in json.loads(ctx["affiliate_takeback"])],
                         [evening.id])
        self.assertNotIn(morning.id, [l["id"] for l in json.loads(ctx["affiliate_takeback"])])
        self.assertEqual(ctx["takeback_count"], 1)
        self.assertEqual(ctx["takeback_past_count"], 1)
        self.assertEqual(ctx["affiliate_count"], 2)   # the day's stat still counts both

    def test_before_the_day_starts_everything_is_listed(self):
        self._leg(time(8, 0), driver=self.affiliate)
        self._leg(time(18, 0), driver=self.affiliate)
        ctx = self._page_at(6)
        self.assertEqual(ctx["takeback_count"], 2)
        self.assertEqual(ctx["takeback_past_count"], 0)

    def test_direct_takeback_skips_a_driver_whose_shared_car_is_out(self):
        # Carlos shares Angel's SUV. Angel has it for a 10:00 run, so Carlos can't take
        # the 10:30 — his own calendar is empty, but the car isn't there (live 9/29:
        # CarlosG offered nine late-morning arrivals while Leo had their Suburban).
        carlos = self._driver("st_carlos", "Carlos", "inhouse")
        DriverVehicleAssignment.objects.create(
            driver=carlos, date=DAY,
            vehicle=DriverVehicleAssignment.objects.get(driver=self.angel, date=DAY).vehicle)
        self._leg(time(10, 0), driver=self.angel)
        leg = self._leg(time(10, 30), driver=self.affiliate)
        ctx = self._page_at(6)
        row = next(l for l in json.loads(ctx["affiliate_takeback"]) if l["id"] == leg.id)
        self.assertIsNotNone(row["direct_takeback"])
        self.assertNotEqual(row["direct_takeback"]["driver_id"], carlos.id)
        self.assertEqual(row["direct_takeback"]["driver_id"], self.vic.id)

    def test_direct_takeback_never_names_the_wrong_car(self):
        # The Van leg's only in-house fit is Vic; Angel's SUV is never the pick.
        leg = self._leg(time(18, 0), driver=self.affiliate, leg_vehicle=self.van)
        ctx = self._page_at(6)
        row = next(l for l in json.loads(ctx["affiliate_takeback"]) if l["id"] == leg.id)
        self.assertEqual(row["vehicle_type"], "van")
        self.assertIsNotNone(row["direct_takeback"])
        self.assertEqual(row["direct_takeback"]["driver_id"], self.vic.id)

    def test_a_job_the_affiliate_is_already_at_is_left_out(self):
        # 6 PM is still ahead at 4 PM, but their chauffeur is standing at the pickup.
        leg = self._leg(time(18, 0), driver=self.affiliate)
        leg.status = "on-location"
        leg.save(update_fields=["status"])
        ctx = self._page_at(16)
        self.assertEqual(json.loads(ctx["affiliate_takeback"]), [])
        self.assertEqual(ctx["takeback_past_count"], 1)


class NoRestaffingThePastTests(_BoardFixture):
    """A stale page can't take back, or swap away, a run that already happened."""

    def test_takeback_refuses_a_pickup_that_has_gone_by(self):
        leg = self._leg(time(8, 0), driver=self.affiliate)
        with self._at(16):
            r = self._post("execute_takeback", {"date": DAY.isoformat(), "leg_id": leg.id,
                                                "driver_id": self.vic.id})
        self.assertEqual(r.status_code, 409)
        self.assertIn("already under way or gone by", r.json()["error"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.affiliate.id)

    def test_takeback_still_works_ahead_of_the_pickup(self):
        leg = self._leg(time(18, 0), driver=self.affiliate)
        with self._at(16):
            r = self._post("execute_takeback", {"date": DAY.isoformat(), "leg_id": leg.id,
                                                "driver_id": self.vic.id})
        self.assertEqual(r.status_code, 200, r.content)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.vic.id)

    def test_takeback_rechecks_the_shared_car_on_a_stale_page(self):
        # The page offered "Take Back → Carlos" for 10:30; since then his car-share
        # partner Angel got a 10:00. The click must not double-book the one car.
        carlos = self._driver("st_carlos2", "Carlos", "inhouse")
        DriverVehicleAssignment.objects.create(
            driver=carlos, date=DAY,
            vehicle=DriverVehicleAssignment.objects.get(driver=self.angel, date=DAY).vehicle)
        self._leg(time(10, 0), driver=self.angel)
        leg = self._leg(time(10, 30), driver=self.affiliate)
        with self._at(6):
            r = self._post("execute_takeback", {"date": DAY.isoformat(), "leg_id": leg.id,
                                                "driver_id": carlos.id})
        self.assertEqual(r.status_code, 409, r.content)
        self.assertIn("car is with", r.json()["error"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.affiliate.id)

    def test_swap_refuses_moving_a_run_that_already_happened(self):
        leg = self._leg(time(8, 0), driver=self.angel)
        with self._at(16):
            r = self._post("execute_swap", {"date": DAY.isoformat(),
                                            "moves": [{"leg_id": leg.id,
                                                       "to_driver_id": self.vic.id}]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("already under way or gone by", r.json()["error"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.angel.id)

    def _find_swaps(self, target, hh):
        with self._at(hh):
            r = self._post("find_swap_suggestions", {"date": DAY.isoformat(),
                                                     "leg_id": target.id})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_find_swaps_never_moves_a_finished_run(self):
        # The 8 AM Van job only fits Vic, and Vic holds an 8 AM run Angel could take.
        # Before the day starts that swap is fine. At 4 PM Vic's run is done — moving
        # it would hand a finished job (and its pay) to Angel — so there is no swap.
        self._leg(time(8, 0), driver=self.vic)
        target = self._leg(time(8, 0), leg_vehicle=self.van)
        early = self._find_swaps(target, 6)
        self.assertTrue(early["solutions"], early)
        self.assertTrue(any(m["from_driver_id"] == self.vic.id
                            for s in early["solutions"] for m in s["moves"]))
        late = self._find_swaps(target, 16)
        self.assertEqual(late["solutions"], [])
