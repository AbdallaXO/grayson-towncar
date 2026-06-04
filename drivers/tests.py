"""Tests for the driver availability resolver and label helpers.

Run with: ./manage.py test drivers
"""
from datetime import date, time

from django.contrib.auth.models import User
from django.test import TestCase

from drivers.models import Driver, DriverWeeklySchedule, DriverDateOverride, FleetVehicle
from rates.models import Vehicle
from drivers.availability import (
    resolve_effective_availability,
    is_pickup_within_window,
    format_exception_badge,
    availability_block_bands,
    format_shift_preference,
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


class ExceptionBadgeAndBandTests(TestCase):
    """Red-pill text (format_exception_badge) and on-grid timeline band math
    (availability_block_bands) used to make one-time unavailability prominent."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="luis", first_name="Luis")
        cls.driver = Driver.objects.create(
            profile=cls.user, driver_type="inhouse",
            default_shift_type="full_day", default_start_hour=6, default_end_hour=17,
            default_flexible=False,
        )

    def _eff(self, **kwargs):
        defaults = dict(date=date(2026, 5, 22), exception_type="off", reason="day_off")
        defaults.update(kwargs)
        DriverDateOverride.objects.create(driver=self.driver, **defaults)
        return resolve_effective_availability(self.driver, defaults["date"])

    # --- format_exception_badge ------------------------------------------------

    def test_badge_unavailable_window(self):
        eff = self._eff(exception_type="unavailable_window",
                        start_time=time(10, 30), end_time=time(12, 30))
        self.assertEqual(format_exception_badge(eff), "Unavailable 10:30 AM – 12:30 PM")

    def test_badge_available_until(self):
        eff = self._eff(exception_type="available_until", end_time=time(16, 0))
        self.assertEqual(format_exception_badge(eff), "Until 4 PM")

    def test_badge_available_after(self):
        eff = self._eff(exception_type="available_after", start_time=time(12, 0))
        self.assertEqual(format_exception_badge(eff), "After 12 PM")

    def test_badge_available_window(self):
        eff = self._eff(exception_type="available_window",
                        start_time=time(8, 0), end_time=time(14, 0))
        self.assertEqual(format_exception_badge(eff), "Window 8 AM – 2 PM")

    def test_badge_empty_when_off(self):
        eff = self._eff(exception_type="off")
        self.assertEqual(format_exception_badge(eff), "")

    def test_badge_empty_when_no_exception(self):
        eff = resolve_effective_availability(self.driver, date(2026, 5, 21))
        self.assertEqual(format_exception_badge(eff), "")

    # --- availability_block_bands (display_start=6, end=22 -> total=1020) -------
    # A time t maps to ((t.hour-6)*60 + t.minute) / 1020 * 100.

    def test_band_unavailable_window(self):
        eff = self._eff(exception_type="unavailable_window",
                        start_time=time(10, 30), end_time=time(12, 30))
        bands = availability_block_bands(eff, 6, 1020)
        self.assertEqual(len(bands), 1)
        self.assertAlmostEqual(bands[0]["left_pct"], 26.5, places=1)   # 270/1020*100
        self.assertAlmostEqual(bands[0]["width_pct"], 11.8, places=1)  # (390-270)/1020*100
        self.assertEqual(bands[0]["label"], "Unavailable")

    def test_band_available_after_blocks_morning(self):
        eff = self._eff(exception_type="available_after", start_time=time(12, 0))
        bands = availability_block_bands(eff, 6, 1020)
        self.assertEqual(len(bands), 1)
        self.assertAlmostEqual(bands[0]["left_pct"], 0.0, places=1)
        self.assertAlmostEqual(bands[0]["width_pct"], 35.3, places=1)  # 360/1020*100

    def test_band_available_until_blocks_evening(self):
        eff = self._eff(exception_type="available_until", end_time=time(16, 0))
        bands = availability_block_bands(eff, 6, 1020)
        self.assertEqual(len(bands), 1)
        self.assertAlmostEqual(bands[0]["left_pct"], 58.8, places=1)   # 600/1020*100
        self.assertAlmostEqual(bands[0]["width_pct"], 41.2, places=1)  # to 100%

    def test_band_available_window_blocks_both_ends(self):
        eff = self._eff(exception_type="available_window",
                        start_time=time(8, 0), end_time=time(14, 0))
        bands = availability_block_bands(eff, 6, 1020)
        self.assertEqual(len(bands), 2)
        self.assertAlmostEqual(bands[0]["left_pct"], 0.0, places=1)
        self.assertAlmostEqual(bands[0]["width_pct"], 11.8, places=1)  # 120/1020*100
        self.assertAlmostEqual(bands[1]["left_pct"], 47.1, places=1)   # 480/1020*100
        self.assertAlmostEqual(bands[1]["width_pct"], 52.9, places=1)

    def test_band_clamps_to_visible_timeline(self):
        # Block 4:00–7:00 but the timeline starts at 6:00 -> clipped to [6:00, 7:00].
        eff = self._eff(exception_type="unavailable_window",
                        start_time=time(4, 0), end_time=time(7, 0))
        bands = availability_block_bands(eff, 6, 1020)
        self.assertEqual(len(bands), 1)
        self.assertAlmostEqual(bands[0]["left_pct"], 0.0, places=1)
        self.assertAlmostEqual(bands[0]["width_pct"], 5.9, places=1)   # 60/1020*100

    def test_band_empty_when_off(self):
        eff = self._eff(exception_type="off")
        self.assertEqual(availability_block_bands(eff, 6, 1020), [])

    def test_band_empty_when_no_exception(self):
        eff = resolve_effective_availability(self.driver, date(2026, 5, 21))
        self.assertEqual(availability_block_bands(eff, 6, 1020), [])


class VehicleCapabilityTests(TestCase):
    """Driver.can_drive / cert_labels / preferred_vehicle_label + format_shift_preference."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="cap", first_name="Cap")
        cls.driver = Driver.objects.create(profile=cls.user, driver_type="inhouse")
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=4)
        cls.sprinter = Vehicle.objects.create(
            vehicle_type="Van(14 Pax)", capacity=14, luggage_capacity=10,
            requires_certification=True,
        )
        cls.unit = FleetVehicle.objects.create(
            vehicle_number="008", vehicle_type=cls.suv, year=2022, make="Chev", model="Suburban",
        )

    def test_can_drive_unrestricted_type(self):
        self.assertTrue(self.driver.can_drive(self.suv))

    def test_cannot_drive_restricted_without_cert(self):
        self.assertFalse(self.driver.can_drive(self.sprinter))

    def test_can_drive_restricted_once_certified(self):
        self.driver.certified_vehicle_types.add(self.sprinter)
        self.assertTrue(self.driver.can_drive(self.sprinter))

    def test_can_drive_none_type_allowed(self):
        self.assertTrue(self.driver.can_drive(None))

    def test_cert_labels_sprinter(self):
        self.driver.certified_vehicle_types.add(self.sprinter)
        self.assertEqual(self.driver.cert_labels(), ["Sprinter"])

    def test_preferred_vehicle_label_type_and_unit(self):
        self.driver.preferred_vehicle_types.add(self.suv)
        self.driver.preferred_vehicles.add(self.unit)
        self.assertEqual(self.driver.preferred_vehicle_label(), "Suv · #008")

    def test_preferred_vehicle_label_empty(self):
        self.assertEqual(self.driver.preferred_vehicle_label(), "")

    # --- format_shift_preference -----------------------------------------------

    def test_shift_pref_flexible_prefers_mornings(self):
        # "Flexible" is shown separately (badge/icon), so the preference label
        # must not repeat it — just the preference nuance.
        eff = {"is_available": True, "flexible": True, "preferred_shift": "morning"}
        self.assertEqual(format_shift_preference(eff), "Prefers mornings")

    def test_shift_pref_fixed_prefers_nights(self):
        eff = {"is_available": True, "flexible": False, "preferred_shift": "night"}
        self.assertEqual(format_shift_preference(eff), "Prefers nights")

    def test_shift_pref_empty_without_preferred_shift(self):
        eff = {"is_available": True, "flexible": True, "preferred_shift": ""}
        self.assertEqual(format_shift_preference(eff), "")

    def test_shift_pref_empty_when_off(self):
        eff = {"is_available": False, "flexible": True, "preferred_shift": "morning"}
        self.assertEqual(format_shift_preference(eff), "")


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
