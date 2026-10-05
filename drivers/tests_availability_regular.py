"""The availability door applies a confirmed regular shift (structured shifts,
Stage 1, Task 4).

With SchedulerSettings.regular_shift_windows off, a confirmed regular shift only
adds keys that describe it: every legacy key resolve_effective_availability
returns stays exactly what it was. With the switch on, a confirmed working day
becomes a fixed, non-flexible window to the minute (regular_shifts.regular_window),
an Off day is unavailable, approved time off still wins, and a flexible exception
falls back to today's reading. A driver without a confirmed regular shift reads
exactly as today and costs no extra queries (Review Focus 3).

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_availability_regular
"""
from datetime import date, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase

from drivers import regular_shifts as rs
from drivers.availability import is_pickup_within_window, resolve_effective_availability
from drivers.models import Driver, DriverDateOverride, DriverWeeklySchedule, ShiftTemplate
from drivers.regular_shifts import DayShift
from drivers.test_support import RegularShiftCacheMixin, clear_regular_shift_caches

MON = date(2026, 10, 5)                         # a Monday
TUE, WED, THU = (MON + timedelta(days=i) for i in (1, 2, 3))
WEEK = [MON + timedelta(days=i) for i in range(7)]
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_LEGACY_KEYS = ("is_available", "shift_type", "start_hour", "end_hour", "flexible", "max_hours",
                "preferred_shift", "preference", "status", "display_label", "tooltip")
_WINDOW_KEYS = ("window_start_min", "window_end_min", "window_kind", "window_max_span_min")
# The usual legacy setting: an open, flexible full day. The switch has to undo it.
_FLEX = dict(default_shift_type="full_day", default_start_hour=4, default_end_hour=23,
             default_flexible=True)
_FIXED = dict(default_shift_type="custom", default_start_hour=5, default_end_hour=19,
              default_flexible=False)

_seq = 0


class _Fixture(RegularShiftCacheMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("ar_manager", is_staff=True, is_superuser=True)

    def setUp(self):
        super().setUp()
        self.t = {t.kind: t for t in ShiftTemplate.objects.all()}

    def driver(self, first="Sam", **kw):
        global _seq
        _seq += 1
        user = User.objects.create_user(username=f"ar_{first.lower()}_{_seq}",
                                        first_name=first, last_name="Driver")
        return Driver.objects.create(profile=user, driver_type="inhouse", **kw)

    def week(self, **days):
        """Seven DayShifts; a day given as (kind, start, end[, options]) works."""
        out = []
        for i, key in enumerate(_DAY_KEYS):
            spec = days.get(key)
            if spec is None:
                out.append(DayShift(i, None, None, None))
                continue
            kind, start, end, *rest = spec
            extra = dict(rest[0]) if rest else {}
            if "alt" in extra:
                extra["alt_template_id"] = self.t[extra.pop("alt")].id
            out.append(DayShift(i, self.t[kind].id, start, end, **extra))
        return out

    def confirm(self, driver, role=None, **days):
        """Confirm a regular shift through the real save; returns a fresh driver."""
        rs.save_regular_shift(driver, self.week(**days), self.manager,
                              role_template_id=self.t[role].id if role else None)
        return Driver.objects.get(pk=driver.pk)

    def switch_on(self):
        with self.assertLogs("drivers.regular_shifts", "INFO"):
            self.assertEqual(rs.set_regular_windows(True, self.manager), (True, ""))

    def legacy(self, eff):
        return {k: eff[k] for k in _LEGACY_KEYS}

    def window(self, eff):
        return tuple(eff[k] for k in _WINDOW_KEYS)


# ════════════════════════════════════════════════════════════════════════════
# Switch off: new keys only
# ════════════════════════════════════════════════════════════════════════════

class SwitchOffTests(_Fixture):
    def test_switch_off_new_keys_only(self):
        d = self.confirm(self.driver("Sam", **_FIXED), role="morning",
                         mon=("morning", time(4, 10), time(15, 30)))
        twin = self.driver("Tom", **_FIXED)
        for day in WEEK:
            mine = resolve_effective_availability(d, day)
            theirs = resolve_effective_availability(twin, day)
            with self.subTest(day=day):
                self.assertEqual(self.legacy(mine), self.legacy(theirs))
                self.assertEqual(self.window(mine), (None, None, None, None))
                self.assertEqual(self.window(theirs), (None, None, None, None))
        mon = resolve_effective_availability(d, MON)
        self.assertEqual(mon["regular_shift"], {
            "kind": "morning", "name": "Morning", "start_min": 250, "end_min": 930,
            "label": "Morning 4:10 AM – 3:30 PM"})
        self.assertFalse(mon["regular_day_off"])
        tue = resolve_effective_availability(d, TUE)
        self.assertIsNone(tue["regular_shift"])
        self.assertTrue(tue["regular_day_off"])
        other = resolve_effective_availability(twin, MON)
        self.assertIsNone(other["regular_shift"])
        self.assertFalse(other["regular_day_off"])
        self.assertEqual(other["shift_role_label"], "")

    def test_hard_limits_on_every_result(self):
        d = self.driver(hard_earliest_start=time(5), hard_latest_finish=time(1),
                        hard_latest_finish_next_day=True)
        eff = resolve_effective_availability(d, MON)
        self.assertEqual((eff["hard_earliest_start"], eff["hard_latest_finish"],
                          eff["hard_latest_finish_next_day"]), (time(5), time(1), True))
        plain = resolve_effective_availability(self.driver("Tom"), MON)
        self.assertEqual((plain["hard_earliest_start"], plain["hard_latest_finish"],
                          plain["hard_latest_finish_next_day"]), (None, None, False))

    def test_role_label_in_eff(self):
        d = self.confirm(self.driver(**_FLEX), role="morning",
                         mon=("morning", time(4, 10), time(15, 30)))
        for day in WEEK:
            self.assertEqual(resolve_effective_availability(d, day)["shift_role_label"],
                             "Morning driver")
            self.assertEqual(resolve_effective_availability(d, day, regular_windows=True)
                             ["shift_role_label"], "Morning driver")
        f = self.confirm(self.driver("Fay", **_FLEX), role="float", mon=("float", None, None))
        self.assertEqual(resolve_effective_availability(f, MON)["shift_role_label"],
                         "Float — any shift")
        self.assertEqual(resolve_effective_availability(self.driver("Una"), MON)
                         ["shift_role_label"], "")


# ════════════════════════════════════════════════════════════════════════════
# Switch on: the regular shift is the day's hours
# ════════════════════════════════════════════════════════════════════════════

class SwitchOnTests(_Fixture):
    def test_switch_on_morning_envelope(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)))
        self.switch_on()
        eff = resolve_effective_availability(d, MON)
        self.assertEqual((eff["window_start_min"], eff["window_end_min"]), (250, 970))
        self.assertEqual((eff["window_kind"], eff["window_max_span_min"]), ("morning", 720))
        self.assertEqual((eff["start_hour"], eff["end_hour"]), (4, 17))
        self.assertIs(eff["is_available"], True)
        self.assertIs(eff["flexible"], False)
        self.assertEqual(eff["shift_type"], "morning")
        self.assertEqual(eff["status"], "fixed_window")
        self.assertEqual(eff["display_label"], "Morning 4:10 AM – 3:30 PM")
        self.assertEqual(eff["tooltip"], "Regular Morning shift, 4:10 AM – 3:30 PM.")
        # The same as passing the switch in.
        self.assertEqual(resolve_effective_availability(d, MON, regular_windows=True), eff)

    def test_switch_on_evening_envelope_crosses_midnight(self):
        d = self.confirm(self.driver(**_FLEX), tue=("evening", time(14, 15), time(2, 15)))
        eff = resolve_effective_availability(d, TUE, regular_windows=True)
        self.assertEqual((eff["window_start_min"], eff["window_end_min"]), (855, 1575))
        self.assertEqual((eff["start_hour"], eff["end_hour"]), (14, 23))
        self.assertEqual(eff["shift_type"], "evening")
        self.assertEqual(eff["status"], "fixed_window")
        self.assertEqual(eff["display_label"], "Evening 2:15 PM – 2:15 AM")
        self.assertEqual(eff["tooltip"],
                         "Regular Evening shift, 2:15 PM – 2:15 AM (ends next day).")

    def test_switch_on_midday_keeps_both_edges(self):
        d = self.confirm(self.driver(**_FLEX), wed=("midday", time(7), time(19)))
        eff = resolve_effective_availability(d, WED, regular_windows=True)
        self.assertEqual((eff["window_start_min"], eff["window_end_min"]), (420, 1140))
        self.assertEqual((eff["start_hour"], eff["end_hour"]), (7, 19))
        self.assertEqual(eff["shift_type"], "midday")
        self.assertEqual(eff["display_label"], "Midday 7 AM – 7 PM")

    def test_switch_on_off_day(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)))
        # A legacy editor's row with no shape on it is Off too, whatever it says.
        DriverWeeklySchedule.objects.create(driver=d, day_of_week=2, is_available=True,
                                            start_hour=6, end_hour=18, flexible=False)
        d = Driver.objects.get(pk=d.pk)
        self.switch_on()
        for day in (TUE, WED):                       # no row / a row without a shape
            eff = resolve_effective_availability(d, day)
            with self.subTest(day=day):
                self.assertIs(eff["is_available"], False)
                self.assertEqual(eff["status"], "off")
                self.assertEqual(eff["display_label"], "Off")
                self.assertTrue(eff["regular_day_off"])
                self.assertIsNone(eff["regular_shift"])
                self.assertEqual(self.window(eff), (None, None, None, None))
                # Not the default_* open day, and not the legacy row's 6-18.
                self.assertTrue(resolve_effective_availability(d, day, regular_windows=False)
                                ["is_available"])

    def test_switch_on_time_off_wins(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)))
        DriverDateOverride.objects.create(driver=d, date=MON, exception_type="off",
                                          reason="vacation")
        eff = resolve_effective_availability(d, MON, regular_windows=True)
        self.assertIs(eff["is_available"], False)
        self.assertEqual(eff["status"], "off")
        self.assertEqual(eff["display_label"], "Off")
        self.assertEqual(eff["tooltip"], "Driver is off (Vacation).")
        self.assertEqual(self.window(eff), (None, None, None, None))
        self.assertEqual(eff["regular_shift"]["label"], "Morning 4:10 AM – 3:30 PM")

    def test_switch_on_flexible_exception_clears_window(self):
        d = self.confirm(self.driver(**_FIXED), mon=("morning", time(4, 10), time(15, 30)))
        DriverDateOverride.objects.create(driver=d, date=MON, end_date=TUE,
                                          exception_type="flexible", reason="other")
        for day in (MON, TUE):                       # a working day and an Off day
            on = resolve_effective_availability(d, day, regular_windows=True)
            off = resolve_effective_availability(d, day, regular_windows=False)
            with self.subTest(day=day):
                self.assertEqual(self.window(on), (None, None, None, None))
                self.assertEqual(on["status"], "flexible")
                self.assertIs(on["flexible"], True)
                self.assertEqual(self.legacy(on), self.legacy(off))   # today's reading

    def test_switch_on_partial_exception_keeps_window(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)),
                         tue=("morning", time(4, 10), time(15, 30)))
        DriverDateOverride.objects.create(driver=d, date=MON, exception_type="available_until",
                                          end_time=time(12), reason="appointment")
        DriverDateOverride.objects.create(driver=d, date=TUE,
                                          exception_type="unavailable_window",
                                          start_time=time(10), end_time=time(11),
                                          reason="appointment")
        mon = resolve_effective_availability(d, MON, regular_windows=True)
        self.assertEqual(self.window(mon), (250, 970, "morning", 720))
        self.assertEqual(mon["status"], "limited")
        self.assertEqual(mon["display_label"], "Until 12 PM")
        self.assertIs(mon["flexible"], False)
        tue = resolve_effective_availability(d, TUE, regular_windows=True)
        self.assertEqual(self.window(tue), (250, 970, "morning", 720))
        self.assertEqual(tue["display_label"],
                         "Morning 4:10 AM – 3:30 PM · Unavailable 10 AM – 11 AM")

    def test_switch_on_unconfirmed_driver_unchanged(self):
        # Review Focus 3: someone added after the switch went on reads as today.
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)))
        self.switch_on()
        late = self.driver("Lee", **_FIXED)
        DriverWeeklySchedule.objects.create(driver=late, day_of_week=0, is_available=True,
                                            shift_type="custom", start_hour=6, end_hour=18,
                                            flexible=False)
        late = Driver.objects.get(pk=late.pk)
        self.assertEqual([x.pk for x in rs.drivers_without_regular_shift()], [late.pk])
        self.assertTrue(rs.regular_windows_on())
        for day in WEEK:
            eff = resolve_effective_availability(late, day)
            with self.subTest(day=day):
                self.assertEqual(eff, resolve_effective_availability(late, day,
                                                                      regular_windows=False))
                self.assertEqual(eff, resolve_effective_availability(late, day,
                                                                      regular_windows=True))
                self.assertEqual(self.window(eff), (None, None, None, None))
                self.assertIsNone(eff["regular_shift"])
                self.assertFalse(eff["regular_day_off"])
                self.assertEqual(eff["shift_role_label"], "")
        mon = resolve_effective_availability(late, MON)
        self.assertEqual((mon["start_hour"], mon["end_hour"], mon["status"]),
                         (6, 18, "fixed_window"))
        self.assertTrue(resolve_effective_availability(d, MON)["window_start_min"])

    def test_hard_limits_clip_window(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)),
                         tue=("float", None, None))
        # Limits edited later, outside the editor (admin): the window is clipped.
        Driver.objects.filter(pk=d.pk).update(hard_earliest_start=time(4, 30),
                                              hard_latest_finish=time(1),
                                              hard_latest_finish_next_day=True)
        d = Driver.objects.get(pk=d.pk)
        mon = resolve_effective_availability(d, MON, regular_windows=True)
        self.assertEqual((mon["window_start_min"], mon["window_end_min"]), (270, 970))
        self.assertEqual((mon["start_hour"], mon["end_hour"]), (4, 17))
        tue = resolve_effective_availability(d, TUE, regular_windows=True)
        self.assertEqual((tue["window_start_min"], tue["window_end_min"]), (270, 1500))

    def test_switch_on_float_day(self):
        d = self.confirm(self.driver(**_FLEX), mon=("float", None, None),
                         tue=("morning", None, None, {"alt": "evening"}))
        mon = resolve_effective_availability(d, MON, regular_windows=True)
        self.assertEqual(self.window(mon), (180, 1575, "float", 720))
        self.assertEqual(mon["display_label"], "Float")
        self.assertEqual(mon["status"], "fixed_window")
        self.assertEqual(mon["shift_type"], "full_day")
        self.assertIs(mon["flexible"], False)
        self.assertEqual((mon["start_hour"], mon["end_hour"]), (3, 23))
        self.assertEqual(mon["regular_shift"], {
            "kind": "float", "name": "Float", "start_min": 180, "end_min": 1575,
            "label": "Float"})
        self.assertEqual(mon["tooltip"],
                         "Regular Float shift, 3 AM – 2:15 AM (ends next day).")
        tue = resolve_effective_availability(d, TUE, regular_windows=True)
        self.assertEqual(self.window(tue), (180, 1575, "float", 720))
        self.assertEqual(tue["display_label"], "Morning or Evening")
        self.assertEqual(tue["shift_type"], "full_day")
        self.assertEqual(tue["status"], "fixed_window")
        self.assertEqual(tue["regular_shift"]["name"], "Morning or Evening")

    def test_switch_on_day_limit_clips(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(14, 45)),
                         thu=("morning", time(4, 10), time(14, 45), {"day_latest": time(15)}))
        thu = resolve_effective_availability(d, THU, regular_windows=True)
        self.assertEqual((thu["window_start_min"], thu["window_end_min"]), (250, 900))
        self.assertEqual((thu["start_hour"], thu["end_hour"]), (4, 15))
        self.assertEqual(thu["display_label"], "Morning 4:10 AM – 2:45 PM · done by 3 PM")
        mon = resolve_effective_availability(d, MON, regular_windows=True)
        self.assertEqual((mon["window_start_min"], mon["window_end_min"]), (250, 970))


# ════════════════════════════════════════════════════════════════════════════
# Pickup warnings
# ════════════════════════════════════════════════════════════════════════════

class PickupWindowTests(_Fixture):
    def test_pickup_within_regular_window(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)),
                         tue=("evening", time(14, 15), time(2, 15)))
        self.switch_on()
        mon = resolve_effective_availability(d, MON)
        self.assertEqual(is_pickup_within_window(mon, time(4, 9)), (
            False, "Pickup at 4:09 AM is outside the driver's regular shift (4:10 AM–4:10 PM)."))
        self.assertEqual(is_pickup_within_window(mon, time(4, 10)), (True, ""))
        self.assertEqual(is_pickup_within_window(mon, time(16, 9)), (True, ""))
        self.assertFalse(is_pickup_within_window(mon, time(16, 10))[0])
        tue = resolve_effective_availability(d, TUE)
        self.assertEqual(is_pickup_within_window(tue, time(23, 30)), (True, ""))
        self.assertEqual(is_pickup_within_window(tue, time(14, 14)), (
            False, "Pickup at 2:14 PM is outside the driver's regular shift (2:15 PM–2:15 AM)."))
        # Tuesday 01:00 is the early hours of Tuesday, not the end of Tuesday's shift.
        self.assertFalse(is_pickup_within_window(tue, time(1))[0])
        self.assertEqual(is_pickup_within_window(resolve_effective_availability(d, WED),
                                                 time(9)), (False, "Driver is off this date."))
        # Switch off: today's reading (an open, flexible day) — no warning.
        self.assertEqual(is_pickup_within_window(
            resolve_effective_availability(d, MON, regular_windows=False), time(4, 9)), (True, ""))

    def test_pickup_with_partial_exception_checks_both(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)))
        DriverDateOverride.objects.create(driver=d, date=MON, exception_type="available_after",
                                          start_time=time(10), reason="appointment")
        eff = resolve_effective_availability(d, MON, regular_windows=True)
        self.assertEqual(is_pickup_within_window(eff, time(9)), (
            False, "Pickup at 9 AM is before the driver is available (10 AM)."))
        self.assertEqual(is_pickup_within_window(eff, time(12)), (True, ""))
        self.assertEqual(is_pickup_within_window(eff, time(17)), (
            False, "Pickup at 5 PM is outside the driver's regular shift (4:10 AM–4:10 PM)."))


# ════════════════════════════════════════════════════════════════════════════
# Queries (Global Constraints: none new for an unconfirmed driver)
# ════════════════════════════════════════════════════════════════════════════

class QueryTests(_Fixture):
    def _fetched(self, driver):
        return (Driver.objects.prefetch_related("weekly_schedule", "date_overrides")
                .get(pk=driver.pk))

    def test_no_extra_queries_unconfirmed(self):
        u = self.driver("Una", **_FIXED)
        DriverWeeklySchedule.objects.create(driver=u, day_of_week=0, is_available=True,
                                            start_hour=6, end_hour=18, flexible=False)
        DriverDateOverride.objects.create(driver=u, date=TUE, exception_type="off")
        fetched = self._fetched(u)
        clear_regular_shift_caches()
        with self.assertNumQueries(0):
            for day in WEEK:
                resolve_effective_availability(fetched, day)
                resolve_effective_availability(fetched, day, regular_windows=True)

    def test_no_extra_queries_confirmed_warm(self):
        d = self.confirm(self.driver(**_FLEX), role="morning",
                         mon=("morning", time(4, 10), time(15, 30)))
        DriverDateOverride.objects.create(driver=d, date=TUE, exception_type="off")
        fetched = self._fetched(d)
        rs.regular_windows_on()
        rs.templates_by_id()
        with self.assertNumQueries(0):
            for day in WEEK:
                resolve_effective_availability(fetched, day)

    def test_confirmed_cold_cache_reads_switch_and_shapes_once(self):
        d = self.confirm(self.driver(**_FLEX), mon=("morning", time(4, 10), time(15, 30)))
        fetched = self._fetched(d)
        clear_regular_shift_caches()
        with self.assertNumQueries(2):
            for day in WEEK:
                resolve_effective_availability(fetched, day)
