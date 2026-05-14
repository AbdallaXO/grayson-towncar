"""Tests for the driver availability resolver and label helpers.

Run with: ./manage.py test drivers
"""
from datetime import date, time

from django.contrib.auth.models import User
from django.test import TestCase

from drivers.models import Driver, DriverWeeklySchedule, DriverDateOverride
from drivers.availability import (
    resolve_effective_availability,
    is_pickup_within_window,
)


class AvailabilityResolverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="angel", first_name="Angel")
        cls.driver = Driver.objects.create(
            profile=cls.user, driver_type="inhouse",
            default_shift_type="full_day", default_start_hour=4, default_end_hour=23,
            default_flexible=True,
        )

    def _wkly(self, dow, **kwargs):
        defaults = dict(is_available=True, shift_type="full_day", start_hour=4, end_hour=23, flexible=True)
        defaults.update(kwargs)
        return DriverWeeklySchedule.objects.create(driver=self.driver, day_of_week=dow, **defaults)

    def _ovrd(self, **kwargs):
        defaults = dict(date=date(2026, 5, 22), exception_type="off", reason="day_off")
        defaults.update(kwargs)
        return DriverDateOverride.objects.create(driver=self.driver, **defaults)

    # --- Status / label cases --------------------------------------------------

    def test_default_flexible_full_day(self):
        eff = resolve_effective_availability(self.driver, date(2026, 5, 21))
        self.assertEqual(eff["status"], "flexible")
        self.assertEqual(eff["display_label"], "Flexible")
        self.assertTrue(eff["is_available"])
        self.assertFalse(eff["has_exception"])

    def test_weekly_off_no_override_renders_off(self):
        # Friday is off
        self._wkly(4, is_available=False, shift_type="full_day")
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))  # 2026-05-22 = Friday
        self.assertEqual(eff["status"], "off")
        self.assertEqual(eff["display_label"], "Off")

    def test_weekly_fixed_window_renders_available_range(self):
        self._wkly(3, is_available=True, shift_type="custom", start_hour=4, end_hour=17, flexible=False)
        eff = resolve_effective_availability(self.driver, date(2026, 5, 21))  # Thursday
        self.assertEqual(eff["status"], "fixed_window")
        self.assertEqual(eff["display_label"], "Available 4 AM – 5 PM")

    def test_off_override_overrides_flexible_weekly(self):
        ov = self._ovrd(date=date(2026, 5, 22), exception_type="off", reason="vacation", notes="Family trip")
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))
        self.assertEqual(eff["status"], "off")
        self.assertEqual(eff["display_label"], "Off")
        self.assertEqual(eff["exception"], ov)
        self.assertIn("Family trip", eff["notes"])
        self.assertFalse(eff["is_available"])
        self.assertFalse(ov.is_available)  # save() syncs derived flag

    def test_available_until_renders_until_label(self):
        self._ovrd(
            date=date(2026, 5, 22), exception_type="available_until",
            end_time=time(16, 0), notes="Must finish by 4 PM",
        )
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))
        self.assertEqual(eff["status"], "limited")
        self.assertEqual(eff["display_label"], "Until 4 PM")
        self.assertTrue(eff["is_available"])

    def test_available_after_renders_after_label(self):
        self._ovrd(date=date(2026, 5, 23), exception_type="available_after", start_time=time(12, 0))
        eff = resolve_effective_availability(self.driver, date(2026, 5, 23))
        self.assertEqual(eff["display_label"], "After 12 PM")

    def test_available_window_renders_window_label(self):
        self._ovrd(
            date=date(2026, 5, 23), exception_type="available_window",
            start_time=time(8, 0), end_time=time(14, 0),
        )
        eff = resolve_effective_availability(self.driver, date(2026, 5, 23))
        self.assertEqual(eff["display_label"], "Window 8 AM – 2 PM")
        self.assertEqual(eff["status"], "limited")

    def test_unavailable_window_overlays_underlying_label(self):
        self._ovrd(
            date=date(2026, 5, 23), exception_type="unavailable_window",
            start_time=time(10, 0), end_time=time(13, 0),
        )
        eff = resolve_effective_availability(self.driver, date(2026, 5, 23))
        self.assertIn("Unavailable 10 AM – 1 PM", eff["display_label"])
        self.assertEqual(eff["status"], "limited")

    def test_flexible_override_on_off_day(self):
        # Sunday weekly off
        self._wkly(6, is_available=False)
        # Driver volunteered to work
        self._ovrd(date=date(2026, 5, 24), exception_type="flexible", notes="Available if needed")
        eff = resolve_effective_availability(self.driver, date(2026, 5, 24))  # 2026-05-24 = Sunday
        self.assertTrue(eff["is_available"])
        self.assertEqual(eff["status"], "flexible")
        self.assertEqual(eff["display_label"], "Flexible")

    def test_note_only_keeps_underlying(self):
        self._ovrd(date=date(2026, 5, 21), exception_type="note_only", notes="No airport runs after 8 PM")
        eff = resolve_effective_availability(self.driver, date(2026, 5, 21))
        self.assertEqual(eff["status"], "flexible")
        self.assertIn("airport runs", eff["notes"])

    # --- Date range / priority cases ------------------------------------------

    def test_range_exception_covers_each_date(self):
        self._ovrd(date=date(2026, 5, 22), end_date=date(2026, 5, 25), exception_type="off", reason="vacation")
        for day in (22, 23, 24, 25):
            eff = resolve_effective_availability(self.driver, date(2026, 5, day))
            self.assertEqual(eff["display_label"], "Off", f"failed on day {day}")
        # Day 26 is outside the range
        eff = resolve_effective_availability(self.driver, date(2026, 5, 26))
        self.assertNotEqual(eff["display_label"], "Off")

    def test_single_date_wins_over_range(self):
        # Range off May 22-25
        self._ovrd(date=date(2026, 5, 22), end_date=date(2026, 5, 25), exception_type="off")
        # Single-date "available_until" on May 23 — should win
        self._ovrd(
            date=date(2026, 5, 23), exception_type="available_until",
            end_time=time(16, 0), notes="Coming in for half-day",
        )
        eff = resolve_effective_availability(self.driver, date(2026, 5, 23))
        self.assertEqual(eff["display_label"], "Until 4 PM")
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))
        self.assertEqual(eff["display_label"], "Off")

    # --- Window check (warning generation) ------------------------------------

    def test_pickup_after_until_cutoff_flags(self):
        self._ovrd(
            date=date(2026, 5, 22), exception_type="available_until",
            end_time=time(16, 0),
        )
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))
        ok, reason = is_pickup_within_window(eff, time(18, 0))
        self.assertFalse(ok)
        self.assertIn("4 PM", reason)

    def test_pickup_inside_unavailable_window_flags(self):
        self._ovrd(
            date=date(2026, 5, 22), exception_type="unavailable_window",
            start_time=time(10, 0), end_time=time(13, 0),
        )
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))
        ok, reason = is_pickup_within_window(eff, time(11, 30))
        self.assertFalse(ok)
        self.assertIn("blocked", reason.lower())

    def test_pickup_outside_blocked_window_passes(self):
        self._ovrd(
            date=date(2026, 5, 22), exception_type="unavailable_window",
            start_time=time(10, 0), end_time=time(13, 0),
        )
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))
        ok, reason = is_pickup_within_window(eff, time(15, 0))
        self.assertTrue(ok)

    def test_pickup_when_driver_is_off_flags(self):
        self._ovrd(date=date(2026, 5, 22), exception_type="off")
        eff = resolve_effective_availability(self.driver, date(2026, 5, 22))
        ok, reason = is_pickup_within_window(eff, time(9, 0))
        self.assertFalse(ok)
        self.assertIn("off", reason.lower())

    # --- Legacy shim still works ----------------------------------------------

    def test_get_availability_for_date_tuple_shim(self):
        self._ovrd(date=date(2026, 5, 22), exception_type="off")
        is_avail, sh, eh, pref, flex = self.driver.get_availability_for_date(date(2026, 5, 22))
        self.assertFalse(is_avail)
        self.assertEqual((sh, eh), (0, 0))


class DriverDateOverrideModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="bob")
        cls.driver = Driver.objects.create(profile=cls.user, driver_type="inhouse")

    def test_save_syncs_is_available_flag(self):
        ov = DriverDateOverride.objects.create(driver=self.driver, date=date(2026, 5, 22), exception_type="off")
        self.assertFalse(ov.is_available)
        ov.exception_type = "available_until"
        ov.end_time = time(16, 0)
        ov.save()
        self.assertTrue(ov.is_available)

    def test_date_range_display_single_day(self):
        ov = DriverDateOverride(driver=self.driver, date=date(2026, 5, 22), exception_type="off")
        self.assertEqual(ov.date_range_display, "May 22, 2026")

    def test_date_range_display_multi_day(self):
        ov = DriverDateOverride(
            driver=self.driver, date=date(2026, 5, 22), end_date=date(2026, 5, 25),
            exception_type="off",
        )
        self.assertEqual(ov.date_range_display, "May 22 – May 25, 2026")

    def test_applies_on_range(self):
        ov = DriverDateOverride(
            driver=self.driver, date=date(2026, 5, 22), end_date=date(2026, 5, 25),
            exception_type="off",
        )
        self.assertTrue(ov.applies_on(date(2026, 5, 22)))
        self.assertTrue(ov.applies_on(date(2026, 5, 24)))
        self.assertTrue(ov.applies_on(date(2026, 5, 25)))
        self.assertFalse(ov.applies_on(date(2026, 5, 26)))
        self.assertFalse(ov.applies_on(date(2026, 5, 21)))
