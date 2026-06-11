"""Farm-Out Optimizer APPLY-path tests — the page's write endpoint.

Run with:  ./manage.py test dispatching.tests_farmout_apply

What must hold:
  * Happy paths: farm_direct (suggested AND founder-override affiliate), opportunity_swap
    (keep target in-house + farm the displaced leg), keep_unassign, free_rescue — each writes
    through set_leg_driver so pay auto-fills from the REAL rate card.
  * STALENESS guard: any drift between the plan's ``expected`` map and live Leg.driver => 409,
    nothing written.
  * HARD RULES: VIP and true-departure legs are never farmed (400).
  * AFFILIATE guards: not-rate-ready affiliate rejected; count_cap affiliate at their daily
    cap rejected (409) against their REAL assigned day.
  * LIVE FEASIBILITY: a keep/move that would double-book the receiving driver => 409, atomic
    rollback (nothing written).
  * HELD DAY: with an active draft + granted user the plan stages into the overlay
    (held=true; live Leg.driver untouched — the sandbox no-leak invariant).
"""
import json
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.farmout_actions import farmout_page_cache_version
from dispatching.scheduler import preload_timing_cache
from drivers.models import (AffiliateProfile, Driver, DriverPayRate, DriverVehicleAssignment,
                            FleetVehicle)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, DraftAssignment, Leg, Reservation, ScheduleDraft

FUTURE = timezone.localdate() + timedelta(days=7)


class _FarmoutApplyFixture(TestCase):
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
            first_name="John", last_name="Doe", email="john@example.com",
            phone_number="5551234567")

        # In-house driver with a towncar on the test day (deployable + vehicle-compatible).
        cls.sam = Driver.objects.create(
            profile=User.objects.create_user("fo_sam", first_name="Sam"),
            driver_type="inhouse")
        fleet = FleetVehicle.objects.create(
            vehicle_number="T-1", vehicle_type=cls.vehicle, year=2024,
            make="Lincoln", model="Continental")
        DriverVehicleAssignment.objects.create(driver=cls.sam, date=FUTURE, vehicle=fleet)

        # Affiliates: Waleed (single chain, $70 flat) is cheapest; Anthony (count cap 2, $90).
        cls.waleed = Driver.objects.create(
            profile=User.objects.create_user("fo_waleed", first_name="Waleed"),
            driver_type="affiliate")
        DriverPayRate.objects.create(driver=cls.waleed, route=cls.route, vehicle=None,
                                     direction="both", base_pay=Decimal("70.00"))
        AffiliateProfile.objects.create(driver=cls.waleed, capacity_mode="single_chain",
                                        max_vehicle_tier="suv")
        cls.anthony = Driver.objects.create(
            profile=User.objects.create_user("fo_anthony", first_name="Anthony"),
            driver_type="affiliate")
        DriverPayRate.objects.create(driver=cls.anthony, route=cls.route, vehicle=None,
                                     direction="both", base_pay=Decimal("90.00"))
        AffiliateProfile.objects.create(driver=cls.anthony, capacity_mode="count_cap",
                                        daily_cap=2)

        cls.staff = User.objects.create_user("fo_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _leg(self, pickup_time=time(9, 0), driver=None, **kw):
        res_kw = kw.pop("reservation_kw", {})
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"), **res_kw)
        defaults = dict(
            reservation=res, pickup_date=FUTURE, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _apply(self, payload):
        return self.client.post(reverse("farmout_apply"), json.dumps(payload),
                                content_type="application/json")

    @staticmethod
    def _farm_plan(leg, affiliate, expected_driver=None):
        return {"kind": "farm_direct", "date": FUTURE.isoformat(),
                "target_leg_id": leg.id, "farm_affiliate_id": affiliate.id,
                "expected": {str(leg.id): expected_driver}}


class FarmDirectTests(_FarmoutApplyFixture):
    def test_farm_to_suggested_affiliate_pays_from_real_card(self):
        leg = self._leg()
        resp = self._apply(self._farm_plan(leg, self.waleed))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertFalse(body["held"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.waleed.id)
        self.assertEqual(leg.driver_base_pay, Decimal("70.00"))

    def test_farm_to_override_affiliate_is_allowed_even_if_pricier(self):
        leg = self._leg()
        resp = self._apply(self._farm_plan(leg, self.anthony))
        self.assertEqual(resp.status_code, 200, resp.content)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.anthony.id)
        self.assertEqual(leg.driver_base_pay, Decimal("90.00"))

    def test_stale_expected_assignment_is_409_and_writes_nothing(self):
        leg = self._leg(driver=self.sam)  # someone assigned it after the page rendered
        resp = self._apply(self._farm_plan(leg, self.waleed, expected_driver=None))
        self.assertEqual(resp.status_code, 409)
        self.assertIn("re-run analyze", resp.json()["error"].lower())
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.sam.id)

    def test_vip_leg_is_never_farmed(self):
        leg = self._leg(reservation_kw={"is_vip": True})
        resp = self._apply(self._farm_plan(leg, self.waleed))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("vip", resp.json()["error"].lower())
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_departure_leg_is_never_farmed(self):
        leg = self._leg(pickup_location="Disney",
                        dropoff_location="Orlando International Airport (MCO)")
        resp = self._apply(self._farm_plan(leg, self.waleed))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("departure", resp.json()["error"].lower())
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_count_cap_affiliate_at_daily_cap_is_409(self):
        self._leg(pickup_time=time(8, 0), driver=self.anthony)
        self._leg(pickup_time=time(12, 0), driver=self.anthony)  # cap = 2, now full
        leg = self._leg(pickup_time=time(15, 0))
        resp = self._apply(self._farm_plan(leg, self.anthony))
        self.assertEqual(resp.status_code, 409)
        self.assertIn("daily cap", resp.json()["error"].lower())
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_uncarded_affiliate_is_rejected(self):
        uncarded = Driver.objects.create(
            profile=User.objects.create_user("fo_uncarded"), driver_type="affiliate")
        leg = self._leg()
        resp = self._apply(self._farm_plan(leg, uncarded))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("rate", resp.json()["error"].lower())

    def test_cache_version_bumps_on_apply(self):
        cache.clear()
        leg = self._leg()
        self.assertEqual(farmout_page_cache_version(FUTURE), 0)
        self._apply(self._farm_plan(leg, self.waleed))
        self.assertEqual(farmout_page_cache_version(FUTURE), 1)

    def test_non_staff_is_403(self):
        self.client.force_login(User.objects.create_user("fo_pleb", password="x"))
        resp = self._apply(self._farm_plan(self._leg(), self.waleed))
        self.assertEqual(resp.status_code, 403)


class SwapAndKeepTests(_FarmoutApplyFixture):
    def test_opportunity_swap_keeps_target_and_farms_displaced(self):
        displaced = self._leg(pickup_time=time(9, 0), driver=self.sam)
        target = self._leg(pickup_time=time(9, 0))  # conflicts with displaced on Sam
        resp = self._apply({
            "kind": "opportunity_swap", "date": FUTURE.isoformat(),
            "target_leg_id": target.id, "keep_driver_id": self.sam.id,
            "farm_leg_id": displaced.id, "farm_affiliate_id": self.waleed.id,
            "expected": {str(target.id): None, str(displaced.id): self.sam.id}})
        self.assertEqual(resp.status_code, 200, resp.content)
        target.refresh_from_db(); displaced.refresh_from_db()
        self.assertEqual(target.driver_id, self.sam.id)
        self.assertEqual(target.driver_base_pay, Decimal("50.00"))   # route in-house pay
        self.assertEqual(displaced.driver_id, self.waleed.id)
        self.assertEqual(displaced.driver_base_pay, Decimal("70.00"))  # Waleed's card

    def test_keep_unassign_leaves_displaced_unassigned(self):
        displaced = self._leg(pickup_time=time(9, 0), driver=self.sam)
        target = self._leg(pickup_time=time(9, 0))
        resp = self._apply({
            "kind": "keep_unassign", "date": FUTURE.isoformat(),
            "target_leg_id": target.id, "keep_driver_id": self.sam.id,
            "farm_leg_id": displaced.id,
            "expected": {str(target.id): None, str(displaced.id): self.sam.id}})
        self.assertEqual(resp.status_code, 200, resp.content)
        target.refresh_from_db(); displaced.refresh_from_db()
        self.assertEqual(target.driver_id, self.sam.id)
        self.assertIsNone(displaced.driver_id)

    def test_free_rescue_assigns_target_into_gap(self):
        target = self._leg(pickup_time=time(9, 0))
        resp = self._apply({
            "kind": "free_rescue", "date": FUTURE.isoformat(),
            "target_leg_id": target.id, "keep_driver_id": self.sam.id,
            "moves": [[target.id, self.sam.id]],
            "expected": {str(target.id): None}})
        self.assertEqual(resp.status_code, 200, resp.content)
        target.refresh_from_db()
        self.assertEqual(target.driver_id, self.sam.id)

    def test_free_rescue_that_double_books_is_409_and_atomic(self):
        self._leg(pickup_time=time(9, 0), driver=self.sam)  # Sam is busy at 9
        target = self._leg(pickup_time=time(9, 0))
        resp = self._apply({
            "kind": "free_rescue", "date": FUTURE.isoformat(),
            "target_leg_id": target.id, "keep_driver_id": self.sam.id,
            "moves": [[target.id, self.sam.id]],
            "expected": {str(target.id): None}})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("infeasible", resp.json()["error"].lower())
        target.refresh_from_db()
        self.assertIsNone(target.driver_id)

    def test_keep_receiver_must_be_active_inhouse(self):
        target = self._leg()
        resp = self._apply({
            "kind": "free_rescue", "date": FUTURE.isoformat(),
            "target_leg_id": target.id, "keep_driver_id": self.waleed.id,
            "moves": [[target.id, self.waleed.id]],
            "expected": {str(target.id): None}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("in-house", resp.json()["error"].lower())


class HardRuleAndPullbackTests(_FarmoutApplyFixture):
    def test_keep_unassign_never_displaces_a_vip_leg(self):
        displaced = self._leg(pickup_time=time(9, 0), driver=self.sam,
                              reservation_kw={"is_vip": True})
        target = self._leg(pickup_time=time(9, 0))
        resp = self._apply({
            "kind": "keep_unassign", "date": FUTURE.isoformat(),
            "target_leg_id": target.id, "keep_driver_id": self.sam.id,
            "farm_leg_id": displaced.id,
            "expected": {str(target.id): None, str(displaced.id): self.sam.id}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("never displaced", resp.json()["error"])
        displaced.refresh_from_db()
        self.assertEqual(displaced.driver_id, self.sam.id)

    def test_pullback_of_committed_farmout_requires_explicit_confirmation(self):
        target = self._leg(pickup_time=time(9, 0), driver=self.waleed)  # committed farm-out
        plan = {"kind": "free_rescue", "date": FUTURE.isoformat(),
                "target_leg_id": target.id, "keep_driver_id": self.sam.id,
                "moves": [[target.id, self.sam.id]],
                "expected": {str(target.id): self.waleed.id}}
        resp = self._apply(plan)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("confirm", resp.json()["error"].lower())
        target.refresh_from_db()
        self.assertEqual(target.driver_id, self.waleed.id)

        resp = self._apply({**plan, "confirm_pullback": True})
        self.assertEqual(resp.status_code, 200, resp.content)
        target.refresh_from_db()
        self.assertEqual(target.driver_id, self.sam.id)

    def test_past_service_dates_are_rejected(self):
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        resp = self._apply({"kind": "farm_direct", "date": past, "target_leg_id": 1,
                            "farm_affiliate_id": self.waleed.id, "expected": {"1": None}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("past", resp.json()["error"].lower())


class MinivanPayParityTests(_FarmoutApplyFixture):
    """The minivan == SUV pricing equivalence must hold at WRITE time, not just in the quote:
    a farmed minivan leg books the affiliate's SUV rate when they card no minivan row, and an
    explicit minivan row always wins over the fallback."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.minivan = Vehicle.objects.create(vehicle_type="mini_van", capacity=7,
                                             luggage_capacity=6)
        # Per-vehicle-carded affiliate: SUV row ONLY (no minivan row, no flat NULL-vehicle row).
        cls.cheapo = Driver.objects.create(
            profile=User.objects.create_user("fo_cheapo", first_name="Cheapo"),
            driver_type="affiliate")
        DriverPayRate.objects.create(driver=cls.cheapo, route=cls.route, vehicle=cls.suv,
                                     direction="both", base_pay=Decimal("85.00"))

    def setUp(self):
        super().setUp()
        from dispatching import farmout_optimizer as fo
        fo._SUV_VEHICLE_CACHE.clear()  # module-level cache must not leak stale Vehicle pks

    def _minivan_leg(self, **kw):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.minivan, base_price=Decimal("150.00"),
            total_price=Decimal("150.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=FUTURE, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", **kw)

    def test_minivan_farm_books_suv_rate_when_no_minivan_row(self):
        leg = self._minivan_leg()
        resp = self._apply(self._farm_plan(leg, self.cheapo))
        self.assertEqual(resp.status_code, 200, resp.content)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.cheapo.id)
        self.assertEqual(leg.driver_base_pay, Decimal("85.00"))  # SUV-rate fallback, not NULL

    def test_explicit_minivan_row_beats_the_suv_fallback(self):
        DriverPayRate.objects.create(driver=self.cheapo, route=self.route,
                                     vehicle=self.minivan, direction="both",
                                     base_pay=Decimal("80.00"))
        leg = self._minivan_leg()
        resp = self._apply(self._farm_plan(leg, self.cheapo))
        self.assertEqual(resp.status_code, 200, resp.content)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_base_pay, Decimal("80.00"))


class PageRenderTests(_FarmoutApplyFixture):
    def test_page_renders_actionable_farm_rows_with_embedded_plans(self):
        cache.clear()
        self._leg(pickup_time=time(13, 0), driver=self.sam)  # Sam's built day (13:00 span)
        self._leg(pickup_time=time(20, 0))                   # leftover outside Sam's span
        resp = self.client.get(reverse("farmout_optimizer"), {"date": FUTURE.isoformat()})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("farm_direct", html)   # embedded apply plan payload
        self.assertIn("Farm it", html)       # per-row farm action
        self.assertIn("Waleed", html)        # affiliate option from the real rate card

    def test_page_renders_already_farmed_rows_without_actions(self):
        cache.clear()
        self._leg(pickup_time=time(13, 0), driver=self.sam)
        self._leg(pickup_time=time(20, 0), driver=self.waleed)  # committed farm-out, no rec
        resp = self.client.get(reverse("farmout_optimizer"), {"date": FUTURE.isoformat()})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Already farmed", resp.content.decode())


class HeldDayTests(_FarmoutApplyFixture):
    def test_apply_on_held_day_stages_into_draft(self):
        granted = User.objects.create_user("fo_granted", password="x", is_staff=True)
        granted.user_permissions.add(
            Permission.objects.get(codename="use_schedule_sandbox"))
        granted = User.objects.get(pk=granted.pk)  # fresh perms cache
        self.client.force_login(granted)

        draft = ScheduleDraft.objects.create(
            schedule_date=FUTURE, state=ScheduleDraft.State.DRAFT, created_by=granted)
        leg = self._leg()
        resp = self._apply(self._farm_plan(leg, self.waleed))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["held"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)  # THE no-leak invariant: live untouched
        da = DraftAssignment.objects.get(draft=draft, leg=leg)
        self.assertEqual(da.proposed_driver_id, self.waleed.id)

        # Staleness on a held day validates against the OVERLAY, not the live board:
        # a second plan still claiming "unassigned" is stale (the draft says Waleed)...
        resp = self._apply(self._farm_plan(leg, self.anthony))
        self.assertEqual(resp.status_code, 409)
        # ...while one that expects the staged state chains cleanly (in-order Apply-all).
        resp = self._apply(self._farm_plan(leg, self.anthony,
                                           expected_driver=self.waleed.id))
        self.assertEqual(resp.status_code, 200, resp.content)
        da.refresh_from_db()
        self.assertEqual(da.proposed_driver_id, self.anthony.id)
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)  # live still untouched
