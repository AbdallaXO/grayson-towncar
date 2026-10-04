"""Driver facts, shift templates and the regular-shift switch (structured shifts, Stage 1).

Covers the model layer only: the seeded Morning / Midday / Evening templates and
their 12-hour ceiling, the minute helpers on DriverWeeklySchedule and Driver, and
the guard that keeps SchedulerSettings.regular_shift_windows out of the generic
settings endpoint and "Reset to defaults" (only the Regular Shifts page may write it).

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_shift_facts
"""
import json
from datetime import time
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from dispatching.models import REGULAR_WINDOWS_CACHE_KEY, SchedulerSettings
from drivers.models import Driver, DriverWeeklySchedule, ShiftTemplate
from drivers.test_support import RegularShiftCacheMixin


def _driver(username="sam", **kw):
    user = User.objects.create_user(username=username, first_name=username.title())
    return Driver.objects.create(profile=user, driver_type="inhouse", **kw)


class ShiftTemplateTests(RegularShiftCacheMixin, TestCase):
    def test_seeded_templates(self):
        self.assertEqual(
            list(ShiftTemplate.objects.values_list("kind", "start_earliest", "start_latest",
                                                   "end_earliest", "end_latest", "max_span_minutes")),
            [("morning", time(3), time(6), time(12), time(16), 720),
             ("midday", time(6), time(9), time(15), time(21), 720),
             ("evening", time(12), time(16), time(20), time(2, 15), 720)])
        self.assertEqual(list(ShiftTemplate.objects.values_list("name", flat=True)),
                         ["Morning", "Midday", "Evening"])
        self.assertTrue(all(ShiftTemplate.objects.values_list("notes", flat=True)))

    def test_template_span_ceiling(self):
        t = ShiftTemplate.objects.get(kind="morning")
        t.max_span_minutes = 721
        with self.assertRaises(ValidationError) as cm:
            t.full_clean()
        self.assertIn("max_span_minutes", cm.exception.message_dict)

    def test_template_start_band_must_run_forwards(self):
        t = ShiftTemplate.objects.get(kind="morning")
        t.start_earliest, t.start_latest = time(7), time(6)
        with self.assertRaises(ValidationError) as cm:
            t.full_clean()
        self.assertIn("start_latest", cm.exception.message_dict)

    def test_evening_end_band_crosses_midnight(self):
        self.assertEqual(ShiftTemplate.objects.get(kind="evening").end_band_minutes(), (1200, 1575))
        morning = ShiftTemplate.objects.get(kind="morning")
        self.assertEqual(morning.start_band_minutes(), (180, 360))
        self.assertEqual(morning.end_band_minutes(), (720, 960))

    def test_band_label(self):
        self.assertEqual(ShiftTemplate.objects.get(kind="morning").band_label(),
                         "leaves 3 AM–6 AM, back 12 PM–4 PM")
        self.assertEqual(ShiftTemplate.objects.get(kind="evening").band_label(),
                         "leaves 12 PM–4 PM, back 8 PM–2:15 AM")
        self.assertEqual(str(ShiftTemplate.objects.get(kind="midday")), "Midday")


class DriverShiftFactsTests(RegularShiftCacheMixin, TestCase):
    def test_weekly_regular_minutes(self):
        driver = _driver()
        evening = ShiftTemplate.objects.get(kind="evening")
        morning = ShiftTemplate.objects.get(kind="morning")
        late = DriverWeeklySchedule(driver=driver, day_of_week=0, shift_template_id=evening.id,
                                    shift_start=time(14, 15), shift_end=time(2, 15))
        early = DriverWeeklySchedule(driver=driver, day_of_week=1, shift_template_id=morning.id,
                                     shift_start=time(4, 10), shift_end=time(15, 30))
        with self.assertNumQueries(0):          # reads shift_template_id, never the template row
            self.assertEqual(late.regular_minutes(), (855, 1575))
            self.assertEqual(early.regular_minutes(), (250, 930))
        same = DriverWeeklySchedule(driver=driver, day_of_week=2, shift_template_id=morning.id,
                                    shift_start=time(4), shift_end=time(4))
        self.assertEqual(same.regular_minutes(), (240, 1680))   # end at the start = next day
        off = DriverWeeklySchedule(driver=driver, day_of_week=3,
                                   shift_start=time(4, 10), shift_end=time(15, 30))
        self.assertIsNone(off.regular_minutes())
        half = DriverWeeklySchedule(driver=driver, day_of_week=4, shift_template_id=morning.id,
                                    shift_start=time(4, 10))
        self.assertIsNone(half.regular_minutes())

    def test_hard_window_minutes(self):
        self.assertEqual(_driver("a", hard_earliest_start=time(5), hard_latest_finish=time(1),
                                 hard_latest_finish_next_day=True).hard_window_minutes(),
                         (300, 1500))
        self.assertEqual(_driver("b", hard_earliest_start=time(5),
                                 hard_latest_finish=time(23)).hard_window_minutes(),
                         (300, 1380))
        self.assertEqual(_driver("c").hard_window_minutes(), (None, None))
        self.assertEqual(_driver("d", hard_latest_finish=time(0),
                                 hard_latest_finish_next_day=True).hard_window_minutes(),
                         (None, 1440))

    def test_new_driver_defaults(self):
        driver = _driver()
        driver.refresh_from_db()
        self.assertEqual(driver.extra_shift_days, [])
        self.assertFalse(driver.has_regular_shift)
        self.assertIsNone(driver.max_days_per_week)
        self.assertFalse(driver.hard_latest_finish_next_day)

    def test_has_regular_shift_once_confirmed(self):
        from django.utils import timezone
        driver = _driver(regular_shift_confirmed_at=timezone.now())
        self.assertTrue(driver.has_regular_shift)

    def test_days_a_week_limits(self):
        driver = _driver(max_days_per_week=8)
        with self.assertRaises(ValidationError) as cm:
            driver.full_clean()
        self.assertIn("max_days_per_week", cm.exception.message_dict)


class RegularShiftSwitchGuardTests(RegularShiftCacheMixin, TestCase):
    def setUp(self):
        super().setUp()
        staff = User.objects.create_user("boss", password="x", is_staff=True)
        self.client.force_login(staff)

    def _post(self, payload):
        return self.client.post(reverse("update_scheduler_settings"),
                                data=json.dumps(payload), content_type="application/json")

    def test_switch_defaults_off(self):
        self.assertFalse(SchedulerSettings.get_settings().regular_shift_windows)

    def test_generic_settings_endpoint_cannot_flip_switch(self):
        resp = self._post({"regular_shift_windows": 1, "min_turn_buffer": 7})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("regular_shift_windows", resp.json()["updated"])
        self.assertEqual(resp.json()["updated"], ["min_turn_buffer"])
        row = SchedulerSettings.objects.get(pk=1)
        self.assertFalse(row.regular_shift_windows)
        self.assertEqual(row.min_turn_buffer, 7)

    def test_stale_cached_row_cannot_write_switch_back(self):
        SchedulerSettings.get_settings()                    # cache the row (switch False)
        SchedulerSettings.objects.filter(pk=1).update(regular_shift_windows=True)
        self._post({"min_turn_buffer": 7})
        self.assertTrue(SchedulerSettings.objects.get(pk=1).regular_shift_windows)
        self.assertEqual(SchedulerSettings.objects.get(pk=1).min_turn_buffer, 7)

    def test_endpoint_saves_only_the_fields_it_was_sent(self):
        # Another worker flips the switch between this request's read and its save.
        stale = SchedulerSettings.get_settings()
        SchedulerSettings.objects.filter(pk=1).update(regular_shift_windows=True)
        with mock.patch.object(SchedulerSettings, "get_settings", return_value=stale):
            self._post({"min_turn_buffer": 7})
        self.assertTrue(SchedulerSettings.objects.get(pk=1).regular_shift_windows)

    def test_reset_keeps_switch(self):
        SchedulerSettings.get_settings()
        SchedulerSettings.objects.filter(pk=1).update(regular_shift_windows=True, min_turn_buffer=9)
        resp = self._post({"reset": True})
        self.assertEqual(resp.status_code, 200)
        row = SchedulerSettings.objects.get(pk=1)
        self.assertTrue(row.regular_shift_windows)
        self.assertEqual(row.min_turn_buffer, 5)            # everything else still resets

    def test_reset_to_defaults_skips_guarded_fields(self):
        row = SchedulerSettings.get_settings()
        row.regular_shift_windows = True                     # a stale in-memory value
        row.min_turn_buffer = 9
        row.reset_to_defaults()
        fresh = SchedulerSettings.objects.get(pk=1)
        self.assertFalse(fresh.regular_shift_windows)        # never written either way
        self.assertEqual(fresh.min_turn_buffer, 5)
        self.assertIn("regular_shift_windows", SchedulerSettings.GUARDED_FIELDS)

    def test_clear_cache_drops_switch_key(self):
        cache.set(REGULAR_WINDOWS_CACHE_KEY, True, 60)
        SchedulerSettings.clear_cache()
        self.assertIsNone(cache.get(REGULAR_WINDOWS_CACHE_KEY))
