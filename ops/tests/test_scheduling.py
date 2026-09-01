"""Tests for the staff scheduling resolver + scheduled-vs-actual comparison."""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from ops.models import StaffWeeklySchedule, StaffScheduleOverride, TimeClockShift, TimeClockBreak
from ops.scheduling import resolve_staff_schedule, schedule_vs_actual


def _aware(dt):
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _staff(name):
    return User.objects.create_user(username=name, is_staff=True)


def _fresh(user):
    return User.objects.prefetch_related("schedule_overrides", "weekly_schedule_rows").get(pk=user.pk)


MONDAY = datetime(2026, 6, 1).date()  # 2026-06-01 is a Monday (weekday 0)


class ResolverTests(TestCase):
    def setUp(self):
        self.user = _staff("sched1")

    def test_no_schedule(self):
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertEqual(r["kind"], "none")
        self.assertIsNone(r["is_working"])

    def test_weekly_working(self):
        StaffWeeklySchedule.objects.create(
            user=self.user, day_of_week=0, is_working=True, start_time=time(9, 0), end_time=time(17, 0)
        )
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertTrue(r["is_working"])
        self.assertIn("9", r["display_label"])

    def test_weekly_off(self):
        StaffWeeklySchedule.objects.create(user=self.user, day_of_week=0, is_working=False)
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertFalse(r["is_working"])
        self.assertEqual(r["display_label"], "Off")

    def test_single_date_override_beats_weekly(self):
        StaffWeeklySchedule.objects.create(
            user=self.user, day_of_week=0, is_working=True, start_time=time(9), end_time=time(17)
        )
        StaffScheduleOverride.objects.create(user=self.user, date=MONDAY, kind="off")
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertFalse(r["is_working"])
        self.assertEqual(r["kind"], "off")

    def test_custom_hours_override(self):
        StaffScheduleOverride.objects.create(
            user=self.user, date=MONDAY, kind="custom_hours", start_time=time(11), end_time=time(19)
        )
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertTrue(r["is_working"])
        self.assertEqual(r["start_time"], time(11))

    def test_range_override(self):
        StaffScheduleOverride.objects.create(
            user=self.user, date=MONDAY, end_date=MONDAY + timedelta(days=4), kind="off"
        )
        r = resolve_staff_schedule(_fresh(self.user), MONDAY + timedelta(days=2))
        self.assertFalse(r["is_working"])

    def test_single_beats_range(self):
        StaffScheduleOverride.objects.create(
            user=self.user, date=MONDAY, end_date=MONDAY + timedelta(days=4), kind="off"
        )
        d = MONDAY + timedelta(days=2)
        StaffScheduleOverride.objects.create(
            user=self.user, date=d, kind="custom_hours", start_time=time(10), end_time=time(14)
        )
        r = resolve_staff_schedule(_fresh(self.user), d)
        self.assertTrue(r["is_working"])
        self.assertEqual(r["kind"], "custom_hours")


class VsActualTests(TestCase):
    def setUp(self):
        self.user = _staff("sched2")
        StaffWeeklySchedule.objects.create(
            user=self.user, day_of_week=0, is_working=True, start_time=time(9, 0), end_time=time(17, 0)
        )
        self.now = _aware(datetime(2026, 6, 1, 18, 0))

    def _shift(self, in_h, in_m, out_h, out_m):
        return TimeClockShift.objects.create(
            user=self.user,
            clock_in_at=_aware(datetime(2026, 6, 1, in_h, in_m)),
            clock_out_at=_aware(datetime(2026, 6, 1, out_h, out_m)),
        )

    def _status(self):
        return schedule_vs_actual(_fresh(self.user), MONDAY, now=self.now)["status"]

    def test_on_time(self):
        self._shift(9, 0, 17, 0)
        self.assertEqual(self._status(), "on_time")

    def test_late_start(self):
        self._shift(9, 20, 17, 0)
        self.assertEqual(self._status(), "late_start")

    def test_left_early(self):
        self._shift(9, 0, 16, 0)
        self.assertEqual(self._status(), "left_early")

    def test_short(self):
        # On time in/out, but a long unpaid break drops net well below scheduled.
        s = self._shift(9, 0, 17, 0)
        TimeClockBreak.objects.create(
            shift=s,
            break_start_at=_aware(datetime(2026, 6, 1, 11, 0)),
            break_end_at=_aware(datetime(2026, 6, 1, 15, 0)),
        )
        self.assertEqual(self._status(), "short")

    def test_open_shift_midday_not_short(self):
        # Clocked in on time and STILL working mid-shift must NOT read "Short"
        # (worked time only counts up to `now`).
        TimeClockShift.objects.create(
            user=self.user,
            clock_in_at=_aware(datetime(2026, 6, 1, 9, 0)),
            clock_out_at=None,
        )
        midday = _aware(datetime(2026, 6, 1, 11, 0))
        status = schedule_vs_actual(_fresh(self.user), MONDAY, now=midday)["status"]
        self.assertEqual(status, "on_time")

    def test_absent_when_tracked(self):
        self.assertEqual(
            schedule_vs_actual(_fresh(self.user), MONDAY, now=self.now, tracking_since=MONDAY)["status"],
            "absent",
        )

    def test_untracked_before_first_clockin(self):
        # Never clocked in (tracking_since=None) -> a scheduled empty day is
        # "untracked", not "absent" (the clock didn't exist yet).
        self.assertEqual(self._status(), "untracked")

    def test_upcoming_before_shift_start(self):
        early = _aware(datetime(2026, 6, 1, 8, 0))  # before the 9:00 scheduled start
        self.assertEqual(
            schedule_vs_actual(_fresh(self.user), MONDAY, now=early, tracking_since=MONDAY)["status"],
            "upcoming",
        )

    def test_no_schedule(self):
        other = _staff("noplan")
        self.assertEqual(
            schedule_vs_actual(_fresh(other), MONDAY, now=self.now)["status"], "no_schedule"
        )

    def test_extra_when_off(self):
        StaffScheduleOverride.objects.create(user=self.user, date=MONDAY, kind="off")
        self._shift(10, 0, 14, 0)
        self.assertEqual(self._status(), "extra")


class WorkLocationTests(TestCase):
    """The resolver's office/WFH answer: weekly default, one-day override flip."""

    def setUp(self):
        self.user = _staff("sched-loc")
        StaffWeeklySchedule.objects.create(
            user=self.user, day_of_week=0, is_working=True,
            start_time=time(9), end_time=time(17), location="remote",
        )

    def test_weekly_location_surfaces(self):
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertEqual(r["location"], "remote")
        self.assertEqual(r["location_label"], "WFH")
        self.assertFalse(r["location_flipped"])

    def test_override_flips_one_day(self):
        StaffScheduleOverride.objects.create(
            user=self.user, date=MONDAY, kind="note", location="office"
        )
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertEqual(r["location"], "office")
        self.assertTrue(r["location_flipped"])
        # Next Monday is untouched — still their usual WFH.
        r2 = resolve_staff_schedule(_fresh(self.user), MONDAY + timedelta(days=7))
        self.assertEqual(r2["location"], "remote")
        self.assertFalse(r2["location_flipped"])

    def test_blank_override_location_keeps_weekly(self):
        StaffScheduleOverride.objects.create(
            user=self.user, date=MONDAY, kind="custom_hours",
            start_time=time(11), end_time=time(19),
        )
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertEqual(r["location"], "remote")
        self.assertFalse(r["location_flipped"])

    def test_day_off_has_no_location(self):
        StaffScheduleOverride.objects.create(user=self.user, date=MONDAY, kind="off")
        r = resolve_staff_schedule(_fresh(self.user), MONDAY)
        self.assertEqual(r["location"], "")
        self.assertEqual(r["location_label"], "")


class ClockInScheduleCheckTests(TestCase):
    """Which punches count as scheduled vs needing approval."""

    def setUp(self):
        from ops.scheduling import clock_in_schedule_check
        self.check = clock_in_schedule_check
        self.user = _staff("sched-check")
        # Mon 9–5 only; every other weekday has an explicit "off" row.
        StaffWeeklySchedule.objects.create(
            user=self.user, day_of_week=0, is_working=True,
            start_time=time(9), end_time=time(17),
        )
        for dow in range(1, 7):
            StaffWeeklySchedule.objects.create(user=self.user, day_of_week=dow, is_working=False)

    def _at(self, day_offset, h, m=0):
        return _aware(datetime(2026, 6, 1 + day_offset, h, m))

    def test_inside_window_ok(self):
        ok, reason = self.check(_fresh(self.user), at=self._at(0, 10))
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_early_grace_ok(self):
        ok, _ = self.check(_fresh(self.user), at=self._at(0, 8, 40))  # 20 min early
        self.assertTrue(ok)

    def test_too_early_flags(self):
        ok, reason = self.check(_fresh(self.user), at=self._at(0, 7, 0))
        self.assertFalse(ok)
        self.assertIn("outside the scheduled", reason)

    def test_after_end_flags(self):
        ok, _ = self.check(_fresh(self.user), at=self._at(0, 20, 0))
        self.assertFalse(ok)

    def test_day_off_flags(self):
        ok, reason = self.check(_fresh(self.user), at=self._at(1, 10))  # Tuesday
        self.assertFalse(ok)
        self.assertIn("not scheduled", reason)

    def test_booked_off_reason_named(self):
        StaffScheduleOverride.objects.create(
            user=self.user, date=MONDAY, kind="off", reason="vacation"
        )
        ok, reason = self.check(_fresh(self.user), at=self._at(0, 10))
        self.assertFalse(ok)
        self.assertIn("booked off", reason)

    def test_unconfigured_staffer_never_flags(self):
        other = _staff("sched-none")
        ok, _ = self.check(_fresh(other), at=self._at(0, 3))
        self.assertTrue(ok)

    def test_overnight_window_from_yesterday_ok(self):
        # Closer scheduled Mon 8 PM – 2 AM; clocking in at 12:30 AM Tuesday
        # is on schedule even though Tuesday itself reads "off".
        night = _staff("sched-night")
        StaffWeeklySchedule.objects.create(
            user=night, day_of_week=0, is_working=True,
            start_time=time(20), end_time=time(2),
        )
        StaffWeeklySchedule.objects.create(user=night, day_of_week=1, is_working=False)
        ok, _ = self.check(_fresh(night), at=self._at(1, 0, 30))
        self.assertTrue(ok)
