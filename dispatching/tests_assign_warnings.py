"""Warn-only validation on the manual assign path (scheduling redesign, Build 1a).

The contract under test: ``update_leg_assignment`` NEVER blocks — it computes
advisory warnings (turn slack + co-driver car-share) and returns them on the
response as ``warnings: [...]``; the write happens regardless. With the
``manual_assign_warnings`` flag off the computation is skipped entirely and the
response carries an empty list (no behavior change with flags off).

Run with:  ./manage.py test dispatching.tests_assign_warnings
"""
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from dispatching.assign_warnings import compute_manual_assign_warnings
from dispatching.models import SchedulerSettings
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

TD = date(2026, 6, 1)

MCO = "Orlando International Airport (MCO), Jeff Fuqua Blvd, Orlando, FL"
DISNEY = "Disney's Grand Floridian Resort, Lake Buena Vista, FL"
POLY = "Disney's Polynesian Village Resort, Lake Buena Vista, FL"


class AssignWarningsBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        patcher = patch("users.emails.send_internal_confirmation", lambda *a, **k: None)
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("disp", password="x", is_staff=True)
        cls.vtype = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            route=cls.route, vehicle=cls.vtype,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user(username="alex", first_name="Alex"),
            driver_type="inhouse")
        cls.partner = Driver.objects.create(
            profile=User.objects.create_user(username="sam", first_name="Sam"),
            driver_type="inhouse")
        cls.customer = Customer.objects.create(
            first_name="Pat", last_name="Guest", email="pat@example.com",
            phone_number="5550001111")
        cls.reservation = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, vehicle=cls.vtype,
            rate=cls.rate, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )

    def setUp(self):
        SchedulerSettings.clear_cache()
        self.addCleanup(SchedulerSettings.clear_cache)

    def _leg(self, hh, mm, pickup=DISNEY, dropoff=MCO, driver=None, status="confirmed"):
        return Leg.objects.create(
            reservation=self.reservation, pickup_date=TD,
            pickup_time=time(hh, mm), pickup_location=pickup,
            dropoff_location=dropoff, driver=driver, route=self.route,
            status=status,
        )

    def _post_assign(self, leg, driver_id):
        self.client.force_login(self.staff)
        return self.client.post(
            reverse("update_leg_assignment"),
            {"leg_id": leg.id, "field": "driver", "value": driver_id},
            content_type="application/json",
        ).json()


class TurnSlackWarningTests(AssignWarningsBase):
    def test_overlapping_pair_warns_critical_and_never_blocks(self):
        self._leg(9, 0, driver=self.driver)          # 09:00 Disney -> MCO
        new = self._leg(9, 10, pickup=POLY)          # 09:10 pickup, same driver
        resp = self._post_assign(new, self.driver.id)
        self.assertTrue(resp["success"], resp)       # NEVER blocks
        codes = [w["code"] for w in resp["warnings"]]
        self.assertIn("turn_critical", codes, resp["warnings"])
        new.refresh_from_db()
        self.assertEqual(new.driver_id, self.driver.id)   # the write happened

    def test_clean_day_has_no_warnings(self):
        self._leg(8, 0, driver=self.driver)
        new = self._leg(17, 0)
        resp = self._post_assign(new, self.driver.id)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["warnings"], [])

    def test_severity_values_are_presentational_only(self):
        self._leg(9, 0, driver=self.driver)
        new = self._leg(9, 10, pickup=POLY)
        for w in self._post_assign(new, self.driver.id)["warnings"]:
            self.assertIn(w["severity"], ("warning", "info"))
            self.assertTrue(w["text"])

    def test_unassign_carries_empty_warnings(self):
        leg = self._leg(9, 0, driver=self.driver)
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("update_leg_assignment"),
            {"leg_id": leg.id, "field": "driver", "value": ""},
            content_type="application/json",
        ).json()
        self.assertTrue(resp["success"])
        self.assertEqual(resp["warnings"], [])

    def test_flag_off_skips_computation(self):
        cfg = SchedulerSettings.get_settings()
        cfg.manual_assign_warnings = False
        cfg.save()
        SchedulerSettings.clear_cache()
        self._leg(9, 0, driver=self.driver)
        new = self._leg(9, 10, pickup=POLY)
        resp = self._post_assign(new, self.driver.id)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["warnings"], [])

    def test_affiliate_driver_is_skipped(self):
        aff = Driver.objects.create(
            profile=User.objects.create_user(username="acme"),
            driver_type="affiliate")
        self._leg(9, 0, driver=aff)
        new = self._leg(9, 10, pickup=POLY)
        self.assertEqual(compute_manual_assign_warnings(new, aff), [])

    def test_compute_never_raises_on_broken_input(self):
        new = self._leg(9, 0)
        new.pickup_time = None
        self.assertEqual(compute_manual_assign_warnings(new, self.driver), [])


class CarShareWarningTests(AssignWarningsBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.car = FleetVehicle.objects.create(
            vehicle_number="014", year=2023, make="Chevrolet", model="Suburban",
            vehicle_type=cls.vtype)
        DriverVehicleAssignment.objects.create(
            driver=cls.driver, date=TD, vehicle=cls.car)
        DriverVehicleAssignment.objects.create(
            driver=cls.partner, date=TD, vehicle=cls.car)

    def test_overlap_with_partner_block_warns(self):
        # Partner departs Disney -> MCO at 09:00 (occupies ~08:05-09:44 at P75).
        # A 09:30 pickup for our driver on the SAME unit overlaps that block.
        self._leg(9, 0, driver=self.partner)
        new = self._leg(9, 30, pickup=POLY)
        warnings = compute_manual_assign_warnings(new, self.driver)
        codes = [w["code"] for w in warnings]
        self.assertIn("share_overlap", codes, warnings)

    def test_tight_handoff_inside_pad_is_flagged(self):
        # Blocks don't overlap (P75: partner's 09:00 departure block ends 09:44,
        # our 10:50 departure block starts 09:55) but the pickup-to-pickup gap
        # is 110 min — under the 120-min share pad.
        self._leg(9, 0, driver=self.partner)
        new = self._leg(10, 50)
        warnings = compute_manual_assign_warnings(new, self.driver)
        codes = [w["code"] for w in warnings]
        self.assertIn("share_pad", codes, warnings)
        self.assertNotIn("share_overlap", codes, warnings)

    def test_handoff_clearing_the_pad_is_clean(self):
        self._leg(9, 0, driver=self.partner)
        new = self._leg(13, 0)
        warnings = compute_manual_assign_warnings(new, self.driver)
        self.assertEqual([w for w in warnings if w["code"].startswith("share_")], [],
                         warnings)

    def test_interleaving_the_shared_day_warns(self):
        # Partner holds the morning AND the evening; wedging our driver into the
        # middle means the car changes hands twice.
        self._leg(8, 0, driver=self.partner)
        self._leg(19, 0, driver=self.partner, pickup=MCO, dropoff=POLY)
        new = self._leg(13, 0)
        warnings = compute_manual_assign_warnings(new, self.driver)
        codes = [w["code"] for w in warnings]
        self.assertIn("share_interleave", codes, warnings)

    def test_pad_is_live_editable(self):
        cfg = SchedulerSettings.get_settings()
        cfg.vehicle_share_pad_min = 60
        cfg.save()
        SchedulerSettings.clear_cache()
        self._leg(9, 0, driver=self.partner)
        new = self._leg(10, 50)          # 110-min gap now clears a 60-min pad
        warnings = compute_manual_assign_warnings(new, self.driver)
        self.assertNotIn("share_pad", [w["code"] for w in warnings], warnings)

    def test_unshared_unit_no_share_warnings(self):
        DriverVehicleAssignment.objects.filter(driver=self.partner).delete()
        self._leg(9, 0, driver=self.partner)
        new = self._leg(10, 50)
        warnings = compute_manual_assign_warnings(new, self.driver)
        self.assertEqual([w for w in warnings if w["code"].startswith("share_")], [],
                         warnings)


class HeldDayWarningTests(AssignWarningsBase):
    """A held-day edit is STAGED into the sandbox draft overlay, which the
    warning checks cannot see — scoring the live board would contradict the
    draft on screen (false alarms and false silence both). So a staged
    response must carry warnings: [] even when the live rows would fire."""

    def test_staged_edit_carries_no_warnings(self):
        from datetime import timedelta

        from django.contrib.auth.models import Permission
        from django.utils import timezone

        from reservations.models import ScheduleDraft

        future = timezone.localdate() + timedelta(days=7)
        self.staff.user_permissions.add(
            Permission.objects.get(codename="use_schedule_sandbox"))
        ScheduleDraft.objects.create(
            schedule_date=future, state=ScheduleDraft.State.DRAFT,
            created_by=self.staff)

        def leg_on(hh, mm, driver=None):
            return Leg.objects.create(
                reservation=self.reservation, pickup_date=future,
                pickup_time=time(hh, mm), pickup_location=DISNEY,
                dropoff_location=MCO, driver=driver, route=self.route,
                status="confirmed")

        leg_on(9, 0, driver=self.driver)      # live board: driver busy at 9:00
        new = leg_on(9, 10)                   # would fire turn_critical if live
        resp = self._post_assign(new, self.driver.id)
        self.assertTrue(resp["success"], resp)
        self.assertTrue(resp["held"], resp)   # the edit was staged
        self.assertEqual(resp["warnings"], [])
        new.refresh_from_db()
        self.assertIsNone(new.driver_id)      # the overlay held it — no live write
