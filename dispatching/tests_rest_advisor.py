"""Rest Advisor tests — overnight rest awareness (docs/scheduler-automation/rest-advisor-design.md).

Two surfaces:
  * RestScorerTests — the soft marginal penalty in suggest_assignments: between two equal
    same-class drivers competing for an early leg, the better-rested one wins; the tired
    driver still covers it when he's the ONLY option; gap=0 disables it.
  * RestAdvisoryTests — build_rest_advisories verifies the FINAL board: a card for any
    driver whose first pickup violates the minimum, naming a rested same-class alternative
    (or saying none exists). Disabled at gap=0; silent for fully-rested / no-prev drivers.
"""
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from dispatching.scheduler import suggest_assignments
from dispatching.models import SchedulerSettings
from dispatching.rest_advisor import build_rest_advisories
from dispatching.tests_span_caps import _slot, _sched
from dispatching.tests_founder_brain import _leg as _vleg
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Vehicle

User = get_user_model()
TARGET = date(2026, 6, 9)        # _slot builds end times on this date (tests_span_caps.D)
PREV = TARGET - timedelta(days=1)


def _dt(h, m=0, day=PREV):
    return datetime.combine(day, time(h, m))


class _SettingsMixin:
    """Force the singleton to DB defaults (510 min = 8.5h, 40/hr) before each test, and
    drop the module-level cache after so a per-test override never leaks."""
    def setUp(self):
        super().setUp()
        SchedulerSettings.clear_cache()

    def tearDown(self):
        SchedulerSettings.clear_cache()
        super().tearDown()

    def _set_gap_minutes(self, minutes):
        s = SchedulerSettings.get_settings()
        s.rest_min_gap_minutes = minutes
        s.save()
        SchedulerSettings.clear_cache()


# ════════════════════════════════════════════════════════════════════════════
# Scorer — the soft overnight-rest penalty
# ════════════════════════════════════════════════════════════════════════════
class RestScorerTests(_SettingsMixin, TestCase):
    def _winner(self, vtypes, prev_end):
        """Who gets the single 5 AM SUV leg? Empty boards for every driver in vtypes."""
        leg = _vleg(99, 5, vtype="suv", trip="return")
        scheds = {did: _sched(did, []) for did in vtypes}
        out = {s.leg_id: s.suggested_driver_id
               for s in suggest_assignments([leg], scheds, TARGET,
                                            driver_vtypes=vtypes,
                                            prev_end_by_driver=prev_end)}
        return out[99]

    def test_rested_driver_wins_the_dawn_leg(self):
        # Driver 1 cleared 11:30 PM yesterday; driver 2 didn't work (fully rested). Both SUV,
        # both empty -> every other score term ties. The rest penalty must send the leg to 2.
        winner = self._winner({1: "suv", 2: "suv"}, {1: _dt(23, 30)})
        self.assertEqual(winner, 2)

    def test_without_feature_lower_id_wins_the_tie(self):
        # Control: no prev-day map -> feature off -> the arbitrary tie-break (lower id) wins,
        # exactly the bug the rest penalty fixes.
        self.assertEqual(self._winner({1: "suv", 2: "suv"}, None), 1)

    def test_tired_only_driver_still_covered(self):
        # Soft, not a block: driver 1 is the ONLY SUV driver and is under-rested -> he STILL
        # gets the leg (no rival to out-score him). Coverage is never sacrificed.
        self.assertEqual(self._winner({1: "suv"}, {1: _dt(23, 30)}), 1)

    def test_gap_zero_disables_scoring(self):
        # rest_min_gap_minutes = 0 -> penalty off -> tie reverts to lower id even though
        # driver 1 is the tired one.
        self._set_gap_minutes(0)
        self.assertEqual(self._winner({1: "suv", 2: "suv"}, {1: _dt(23, 30)}), 1)

    def test_well_rested_late_finisher_keeps_early_leg(self):
        # Driver 1 cleared 6 PM yesterday (11h rest by 5 AM) -> no deficit -> no penalty ->
        # the tie-break (lower id) keeps the leg on 1. The penalty only bites a real deficit.
        self.assertEqual(self._winner({1: "suv", 2: "suv"}, {1: _dt(18, 0)}), 1)


# ════════════════════════════════════════════════════════════════════════════
# Advisory cards — final-board verification
# ════════════════════════════════════════════════════════════════════════════
def _mk_driver(username):
    u = User.objects.create_user(username=username, password="x")
    return Driver.objects.create(profile=u, driver_type="inhouse", is_active=True)


class RestAdvisoryTests(_SettingsMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.vt_van = Vehicle.objects.create(vehicle_type="van", capacity=10, luggage_capacity=10)
        cls.suv_a = FleetVehicle.objects.create(vehicle_number="001", vehicle_type=cls.vt_suv,
                                                year=2023, make="Chevy", model="Suburban")
        cls.suv_b = FleetVehicle.objects.create(vehicle_number="002", vehicle_type=cls.vt_suv,
                                                year=2023, make="Chevy", model="Suburban")
        cls.van_a = FleetVehicle.objects.create(vehicle_number="010", vehicle_type=cls.vt_van,
                                                year=2022, make="Ford", model="Transit")
        cls.george = _mk_driver("george")
        cls.marcus = _mk_driver("marcus")

    def _assign(self, driver, vehicle):
        DriverVehicleAssignment.objects.create(driver=driver, date=TARGET, vehicle=vehicle)

    def _drivers_by_id(self):
        return {self.george.id: self.george, self.marcus.id: self.marcus}

    def test_violation_emits_card(self):
        # George (SUV) starts 4 AM having cleared 11:30 PM -> 4.5h rest < 8.5h -> one card.
        self._assign(self.george, self.suv_a)
        proposed = {self.george.id: _sched(self.george.id, [_slot(1, 4)])}
        cards = build_rest_advisories(TARGET, proposed, {self.george.id: _dt(23, 30)},
                                      {self.george.id}, self._drivers_by_id())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["signature"], f"_rest{self.george.id}")
        self.assertEqual(cards[0]["kind"], "rest")
        self.assertEqual(cards[0]["rest_hours"], 4.5)
        self.assertIn("george", cards[0]["text"].lower())

    def test_rested_same_class_alternative_named(self):
        # Marcus is a working SUV driver who didn't work yesterday -> George's card offers
        # him as the rested alternative.
        self._assign(self.george, self.suv_a)
        self._assign(self.marcus, self.suv_b)
        proposed = {self.george.id: _sched(self.george.id, [_slot(1, 4)]),
                    self.marcus.id: _sched(self.marcus.id, [_slot(2, 11)])}
        cards = build_rest_advisories(TARGET, proposed, {self.george.id: _dt(23, 30)},
                                      {self.george.id, self.marcus.id}, self._drivers_by_id())
        self.assertEqual(len(cards), 1)
        self.assertIn("marcus", cards[0]["text"].lower())

    def test_no_alternative_text_when_only_driver(self):
        # Marcus is a VAN driver (wrong class) -> no rested SUV alternative -> explicit text.
        self._assign(self.george, self.suv_a)
        self._assign(self.marcus, self.van_a)
        proposed = {self.george.id: _sched(self.george.id, [_slot(1, 4)]),
                    self.marcus.id: _sched(self.marcus.id, [_slot(2, 11)])}
        cards = build_rest_advisories(TARGET, proposed, {self.george.id: _dt(23, 30)},
                                      {self.george.id, self.marcus.id}, self._drivers_by_id())
        self.assertEqual(len(cards), 1)
        self.assertIn("No rested", cards[0]["text"])

    def test_no_prev_day_legs_no_card(self):
        # George has no prev-day entry -> fully rested -> silent even on a 4 AM start.
        self._assign(self.george, self.suv_a)
        proposed = {self.george.id: _sched(self.george.id, [_slot(1, 4)])}
        cards = build_rest_advisories(TARGET, proposed, {}, {self.george.id}, self._drivers_by_id())
        self.assertEqual(cards, [])

    def test_well_rested_driver_no_card(self):
        # 9 AM start after an 11:30 PM clear = 9.5h rest >= 8.5h -> no card.
        self._assign(self.george, self.suv_a)
        proposed = {self.george.id: _sched(self.george.id, [_slot(1, 9)])}
        cards = build_rest_advisories(TARGET, proposed, {self.george.id: _dt(23, 30)},
                                      {self.george.id}, self._drivers_by_id())
        self.assertEqual(cards, [])

    def test_gap_zero_disables_cards(self):
        self._set_gap_minutes(0)
        self._assign(self.george, self.suv_a)
        proposed = {self.george.id: _sched(self.george.id, [_slot(1, 4)])}
        cards = build_rest_advisories(TARGET, proposed, {self.george.id: _dt(23, 30)},
                                      {self.george.id}, self._drivers_by_id())
        self.assertEqual(cards, [])

    def test_prev_end_after_midnight_deficit_correct(self):
        # Yesterday's last leg cleared 12:30 AM (datetime on TARGET) -> a 6 AM start is 5.5h
        # rest. datetime math (not time-of-day) must handle the day rollover.
        self._assign(self.george, self.suv_a)
        proposed = {self.george.id: _sched(self.george.id, [_slot(1, 6)])}
        cards = build_rest_advisories(TARGET, proposed,
                                      {self.george.id: _dt(0, 30, day=TARGET)},
                                      {self.george.id}, self._drivers_by_id())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["rest_hours"], 5.5)
