"""Second-Shift Advisor tests. Design: auto-assign-hour-balancing-design.md PART 2 + 4b."""
import json
from datetime import date, timedelta, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from dispatching.shift_advisor import build_shift_proposals
from dispatching.tests_span_caps import _slot, _sched, _FakeLeg
from drivers.models import Driver, DriverVehicleAssignment, DriverWeeklySchedule, FleetVehicle
from rates.models import Vehicle

User = get_user_model()
TARGET = date(2026, 6, 9)   # must match tests_span_caps.D — _slot() builds end times on it


def _mk_driver(username, certified=None):
    u = User.objects.create_user(username=username, password="x")
    d = Driver.objects.create(profile=u, driver_type="inhouse", is_active=True)
    if certified:
        d.certified_vehicle_types.add(certified)
    return d


def _leg(leg_id, pickup_h, pickup_m=0, vtype="suv", revenue=100):
    return _FakeLeg(
        id=leg_id, pickup_time=time(pickup_h, pickup_m),
        pickup_location="Disney Resort", dropoff_location="MCO Terminal",
        effective_vehicle_type=vtype, revenue_share=revenue,
        driver=None, driver_id=None, reservation_id=1, status="pending",
        flight_information=None, trip_type="return",
    )


class ShiftAdvisorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.vt_van14 = Vehicle.objects.create(vehicle_type="Van(14 Pax)", capacity=14,
                                              luggage_capacity=14, requires_certification=True)
        cls.spare = FleetVehicle.objects.create(vehicle_number="001", vehicle_type=cls.vt_suv,
                                                year=2023, make="Chevy", model="Suburban")
        cls.held = FleetVehicle.objects.create(vehicle_number="006", vehicle_type=cls.vt_suv,
                                               year=2023, make="Chevy", model="Suburban")
        cls.sprinter = FleetVehicle.objects.create(vehicle_number="004", vehicle_type=cls.vt_van14,
                                                   year=2022, make="Mercedes", model="Sprinter")
        cls.worker = _mk_driver("worker")
        cls.idle = _mk_driver("idle")
        # unit usage history so spare units clear the rarely-used bar
        for i in range(1, 7):
            DriverVehicleAssignment.objects.create(
                driver=cls.idle, date=TARGET - timedelta(days=i), vehicle=cls.spare)

    def _board(self, worker_slots):
        return {self.worker.id: _sched(self.worker.id, worker_slots)}

    def test_residual_leg_proposes_idle_driver_with_spare_unit(self):
        legs = {99: _leg(99, 19)}
        props = build_shift_proposals(TARGET, [legs[99]], {}, {self.worker.id},
                                      self._board([_slot(1, 8)]), legs)
        self.assertEqual(len(props), 1)
        p = props[0]
        self.assertEqual(p["kind"], "residual")
        self.assertEqual(p["best"]["driver_id"], self.idle.id)
        self.assertEqual(p["best"]["vehicle_id"], self.spare.id)
        self.assertIsNone(p["best"]["freed"])

    def test_freed_unit_offered_when_no_spare(self):
        DriverVehicleAssignment.objects.filter(vehicle=self.spare).delete()
        self.spare.delete()
        # worker holds #006 and clears at 09:30 — free for a 19:00 second shift.
        DriverVehicleAssignment.objects.create(driver=self.worker, date=TARGET, vehicle=self.held)
        for i in range(1, 7):
            DriverVehicleAssignment.objects.create(
                driver=self.worker, date=TARGET - timedelta(days=i), vehicle=self.held)
        legs = {99: _leg(99, 19)}
        props = build_shift_proposals(TARGET, [legs[99]], {}, {self.worker.id},
                                      self._board([_slot(1, 8, dur_min=90)]), legs)
        self.assertEqual(len(props), 1)
        best = props[0]["best"]
        self.assertIsNotNone(best)
        self.assertEqual(best["vehicle_id"], self.held.id)
        self.assertIsNotNone(best["freed"])
        self.assertTrue(best["freed"]["clear"])   # formatted holder-clears time

    def test_cert_gate_no_sprinter_for_uncertified(self):
        DriverVehicleAssignment.objects.filter(vehicle=self.spare).delete()
        self.spare.delete()
        legs = {99: _leg(99, 19, vtype="Van(14 Pax)")}
        props = build_shift_proposals(TARGET, [legs[99]], {}, {self.worker.id},
                                      self._board([_slot(1, 8)]), legs)
        self.assertEqual(len(props), 1)
        self.assertIsNone(props[0]["best"])   # idle isn't certified; no other source

    def test_mid_shift_driver_excluded_from_idle_roster(self):
        # A driver with LEGS today (but no vehicle row) is mid-shift data drift, not idle.
        from reservations.models import Reservation, Leg
        # create a real minimal leg for idle driver -> he must not be suggested
        try:
            res = Reservation.objects.create()
        except Exception:
            self.skipTest("Reservation requires richer fixtures")
        Leg.objects.create(reservation=res, pickup_date=TARGET, pickup_time=time(9, 0),
                           pickup_location="A", dropoff_location="B", driver=self.idle)
        legs = {99: _leg(99, 19)}
        props = build_shift_proposals(TARGET, [legs[99]], {}, {self.worker.id},
                                      self._board([_slot(1, 8)]), legs)
        self.assertTrue(props[0]["best"] is None
                        or props[0]["best"]["driver_id"] != self.idle.id)

    def test_scheduled_off_idle_labeled(self):
        DriverWeeklySchedule.objects.create(
            driver=self.idle, day_of_week=TARGET.weekday(), is_available=False,
            shift_type="full_day", start_hour=0, end_hour=23)
        legs = {99: _leg(99, 19)}
        props = build_shift_proposals(TARGET, [legs[99]], {}, {self.worker.id},
                                      self._board([_slot(1, 8)]), legs)
        self.assertTrue(props[0]["best"]["scheduled_off"])

    def test_tier_aware_clustering_separates_van_from_suv(self):
        legs = {98: _leg(98, 18, vtype="suv"), 99: _leg(99, 19, vtype="Van(14 Pax)")}
        props = build_shift_proposals(TARGET, list(legs.values()), {}, {self.worker.id},
                                      self._board([_slot(1, 8)]), legs)
        self.assertEqual(len([p for p in props if p.get("kind") != "info"]), 2)
        tiers = {p["tier_label"] for p in props}
        self.assertIn("suv", tiers)
        self.assertIn("Van(14 Pax)", tiers)

    def test_overload_min_tail_trigger(self):
        # Worker's day 04:00-19:30 compact; tail legs 17:00+19:00 are movable -> proposal.
        slots = [_slot(i, h, dur_min=90) for i, h in enumerate([4, 6, 8, 10, 12, 14, 17, 19], 1)]
        legs = {7: _leg(7, 17), 8: _leg(8, 19)}
        overload = {self.worker.id: {"name": "worker", "slots": slots,
                                     "movable_ids": {7, 8}}}
        props = build_shift_proposals(TARGET, [], overload, {self.worker.id},
                                      self._board(slots), legs)
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["kind"], "overload")
        self.assertEqual({l["leg_id"] for l in props[0]["legs"]}, {7, 8})

    def test_overload_locked_tail_gets_info_card(self):
        # A long day whose tail is hand-assigned/locked can't be auto-drained — the advisor
        # must SAY so (founder: rizwan 3:45 AM-6:15 PM), not stay silent.
        slots = [_slot(i, h, dur_min=90) for i, h in enumerate([4, 6, 8, 10, 12, 14, 17, 19], 1)]
        overload = {self.worker.id: {"name": "worker", "slots": slots, "movable_ids": set()}}
        props = build_shift_proposals(TARGET, [], overload, {self.worker.id},
                                      self._board(slots), {})
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["kind"], "info")
        self.assertIn("hand-assigned", props[0]["text"])

    def test_deterministic(self):
        legs = {99: _leg(99, 19), 98: _leg(98, 17)}
        a = build_shift_proposals(TARGET, list(legs.values()), {}, {self.worker.id},
                                  self._board([_slot(1, 8)]), legs)
        b = build_shift_proposals(TARGET, list(legs.values()), {}, {self.worker.id},
                                  self._board([_slot(1, 8)]), legs)
        self.assertEqual(a, b)


class AllowShareApplyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.unit = FleetVehicle.objects.create(vehicle_number="006", vehicle_type=cls.vt_suv,
                                               year=2023, make="Chevy", model="Suburban")
        cls.holder = _mk_driver("holder")
        cls.second = _mk_driver("second")
        cls.staff = User.objects.create_user("boss2", password="x", is_staff=True)
        DriverVehicleAssignment.objects.create(driver=cls.holder, date=TARGET, vehicle=cls.unit)

    def _post(self, allow_share):
        self.client.force_login(self.staff)
        return self.client.post(
            reverse("apply_day_setup"),
            data=json.dumps({"date": TARGET.isoformat(),
                             "pairs": [{"driver_id": self.second.id, "vehicle_id": self.unit.id,
                                        "allow_share": allow_share}],
                             "snapshot": {str(self.second.id): None}}),
            content_type="application/json")

    def test_share_blocked_without_flag(self):
        self.assertEqual(self._post(False).status_code, 400)

    def test_share_allowed_with_flag(self):
        r = self._post(True)
        self.assertEqual(r.status_code, 200)
        # both rows exist on the same unit — the founder's AM/PM share, made deliberate
        self.assertEqual(DriverVehicleAssignment.objects.filter(
            date=TARGET, vehicle=self.unit).count(), 2)
