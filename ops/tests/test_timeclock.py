"""
Tests for the staff time clock (clock in/out + unpaid breaks).

Time-sensitive cases pass an explicit ``now=`` into the service layer instead
of freezing the global clock — same approach as test_unpaid_reminders.py.
"""

import json
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ops.models import (
    TimeClockShift,
    TimeClockBreak,
    StaffWeeklySchedule,
    StaffScheduleOverride,
)
from ops.services import (
    TimeClockError,
    clock_in,
    clock_out,
    start_break,
    end_break,
    get_open_shift,
    auto_close_stale_shifts,
    admin_punch_in,
    admin_punch_out,
    admin_create_shift,
    admin_update_shift,
    admin_delete_shift,
    admin_add_break,
)
from ops.views import _tc_parse_range, _tc_aggregate, _tc_today_worked_seconds


def _aware(dt):
    """Localize a naive datetime to the project tz (America/New_York)."""
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _shift(pk):
    """Re-fetch a shift fresh, with breaks prefetched (clears stale caches)."""
    return TimeClockShift.objects.prefetch_related("breaks").get(pk=pk)


class TimeClockServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dispatch1", is_staff=True)
        self.t9 = _aware(datetime(2026, 5, 20, 9, 0))

    # ── State machine guards ──
    def test_clock_in_then_out_no_break(self):
        clock_in(self.user, now=self.t9)
        s = clock_out(self.user, now=self.t9 + timedelta(hours=8))
        s = _shift(s.pk)
        self.assertFalse(s.is_open)
        self.assertEqual(s.worked_minutes, 480)
        self.assertEqual(s.break_minutes, 0)

    def test_double_clock_in_raises(self):
        clock_in(self.user, now=self.t9)
        with self.assertRaises(TimeClockError):
            clock_in(self.user, now=self.t9 + timedelta(minutes=1))
        self.assertEqual(
            TimeClockShift.objects.filter(user=self.user, clock_out_at__isnull=True).count(),
            1,
        )

    def test_break_before_clock_in_raises(self):
        with self.assertRaises(TimeClockError):
            start_break(self.user, now=self.t9)

    def test_double_break_raises(self):
        clock_in(self.user, now=self.t9)
        start_break(self.user, now=self.t9 + timedelta(hours=3))
        with self.assertRaises(TimeClockError):
            start_break(self.user, now=self.t9 + timedelta(hours=3, minutes=5))

    def test_end_break_without_break_raises(self):
        clock_in(self.user, now=self.t9)
        with self.assertRaises(TimeClockError):
            end_break(self.user, now=self.t9 + timedelta(hours=1))

    def test_clock_out_without_shift_raises(self):
        with self.assertRaises(TimeClockError):
            clock_out(self.user, now=self.t9)

    # ── Worked-time math (breaks are unpaid) ──
    def test_break_excluded_from_worked_time(self):
        clock_in(self.user, now=self.t9)
        start_break(self.user, now=self.t9 + timedelta(hours=3))            # 12:00
        end_break(self.user, now=self.t9 + timedelta(hours=3, minutes=30))  # 12:30
        s = clock_out(self.user, now=self.t9 + timedelta(hours=8))          # 17:00
        s = _shift(s.pk)
        self.assertEqual(s.gross_minutes, 480)
        self.assertEqual(s.break_minutes, 30)
        self.assertEqual(s.worked_minutes, 450)

    def test_two_breaks_sum(self):
        clock_in(self.user, now=self.t9)
        start_break(self.user, now=self.t9 + timedelta(hours=2))
        end_break(self.user, now=self.t9 + timedelta(hours=2, minutes=15))
        start_break(self.user, now=self.t9 + timedelta(hours=5))
        end_break(self.user, now=self.t9 + timedelta(hours=5, minutes=45))
        s = _shift(clock_out(self.user, now=self.t9 + timedelta(hours=8)).pk)
        self.assertEqual(s.break_minutes, 60)
        self.assertEqual(s.worked_minutes, 420)

    def test_open_shift_worked_counts_to_now(self):
        clock_in(self.user, now=self.t9)
        s = _shift(get_open_shift(self.user).pk)
        self.assertEqual(int(s.worked_seconds(self.t9 + timedelta(hours=2)) // 60), 120)

    def test_clock_out_while_on_break_auto_closes_break(self):
        clock_in(self.user, now=self.t9)
        start_break(self.user, now=self.t9 + timedelta(hours=4))
        s = clock_out(self.user, now=self.t9 + timedelta(hours=5))
        s = _shift(s.pk)
        brk = s.breaks.first()
        self.assertIsNotNone(brk.break_end_at)
        self.assertTrue(brk.auto_closed)
        self.assertEqual(s.break_minutes, 60)
        self.assertEqual(s.worked_minutes, 240)  # 5h gross - 1h break

    # ── Queries / cleanup ──
    def test_get_open_shift_only_returns_open(self):
        clock_in(self.user, now=self.t9)
        clock_out(self.user, now=self.t9 + timedelta(hours=8))
        self.assertIsNone(get_open_shift(self.user))
        clock_in(self.user, now=self.t9 + timedelta(days=1))
        self.assertIsNotNone(get_open_shift(self.user))

    def test_auto_close_caps_stale_shift(self):
        now = _aware(datetime(2026, 5, 21, 12, 0))
        stale_user = User.objects.create_user(username="forgot", is_staff=True)
        fresh_user = User.objects.create_user(username="ontime", is_staff=True)
        stale = TimeClockShift.objects.create(
            user=stale_user, clock_in_at=now - timedelta(hours=30)
        )
        fresh = TimeClockShift.objects.create(
            user=fresh_user, clock_in_at=now - timedelta(hours=2)
        )

        closed = auto_close_stale_shifts(max_hours=16, now=now)
        self.assertEqual(closed, 1)

        stale.refresh_from_db()
        self.assertTrue(stale.auto_closed)
        self.assertEqual(stale.clock_out_at, stale.clock_in_at + timedelta(hours=16))

        fresh.refresh_from_db()
        self.assertTrue(fresh.is_open)
        self.assertFalse(fresh.auto_closed)


class TimeClockRangeTests(TestCase):
    """ET day-boundary grouping for the founder report."""

    def setUp(self):
        self.user = User.objects.create_user(username="late", is_staff=True)

    def test_late_night_shift_groups_on_start_day_et(self):
        from django.test import RequestFactory

        # 23:30 ET on May 20 — would be 03:30 UTC on May 21.
        ci = _aware(datetime(2026, 5, 20, 23, 30))
        TimeClockShift.objects.create(
            user=self.user, clock_in_at=ci, clock_out_at=ci + timedelta(hours=2)
        )
        req = RequestFactory().get("/", {"start": "2026-05-20", "end": "2026-05-20"})
        start_date, end_date, _, start_dt, end_dt = _tc_parse_range(req)
        self.assertEqual(start_date.isoformat(), "2026-05-20")
        shifts = TimeClockShift.objects.filter(
            clock_in_at__gte=start_dt, clock_in_at__lt=end_dt
        ).prefetch_related("breaks")
        rows = _tc_aggregate(list(shifts))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shifts"], 1)


class TimeClockTodayTests(TestCase):
    """The dispatcher's 'Today worked' line, including the overnight edge case."""

    def setUp(self):
        self.user = User.objects.create_user(username="owl", is_staff=True)

    def test_today_includes_open_overnight_shift(self):
        # Clocked in 11:31 PM yesterday, still on the clock at 12:03 AM today.
        now = _aware(datetime(2026, 6, 1, 0, 3))
        ci = _aware(datetime(2026, 5, 31, 23, 31))
        TimeClockShift.objects.create(user=self.user, clock_in_at=ci)  # open
        secs = _tc_today_worked_seconds(self.user, now=now)
        self.assertEqual(int(secs // 60), 32)  # live session, NOT 0

    def test_today_counts_completed_shift_started_today(self):
        now = _aware(datetime(2026, 6, 1, 18, 0))
        ci = _aware(datetime(2026, 6, 1, 9, 0))
        TimeClockShift.objects.create(
            user=self.user, clock_in_at=ci, clock_out_at=ci + timedelta(hours=8)
        )
        self.assertEqual(int(_tc_today_worked_seconds(self.user, now=now) // 60), 480)

    def test_today_excludes_yesterday_completed_shift(self):
        now = _aware(datetime(2026, 6, 1, 9, 0))
        ci = _aware(datetime(2026, 5, 31, 9, 0))
        TimeClockShift.objects.create(
            user=self.user, clock_in_at=ci, clock_out_at=ci + timedelta(hours=8)
        )
        self.assertEqual(int(_tc_today_worked_seconds(self.user, now=now) // 60), 0)


class TimeClockViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staff", password="x", is_staff=True)
        self.super = User.objects.create_user(
            username="boss", password="x", is_staff=True, is_superuser=True
        )
        self.plain = User.objects.create_user(username="plain", password="x")

    def test_timeclock_requires_staff(self):
        self.client.force_login(self.plain)
        resp = self.client.get(reverse("timeclock"))
        self.assertEqual(resp.status_code, 302)

    def test_timeclock_page_loads_for_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("timeclock")).status_code, 200)

    def test_action_clock_in_creates_shift(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("timeclock_action"),
            data=json.dumps({"action": "clock_in"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["state"], "clocked_in")
        self.assertTrue(TimeClockShift.objects.filter(user=self.staff, clock_out_at__isnull=True).exists())

    def test_overview_requires_superuser(self):
        self.client.force_login(self.staff)  # staff but not superuser
        self.assertEqual(self.client.get(reverse("timeclock_overview")).status_code, 302)

    def test_overview_loads_for_superuser(self):
        self.client.force_login(self.super)
        self.assertEqual(self.client.get(reverse("timeclock_overview")).status_code, 200)

    def test_csv_export(self):
        ci = _aware(datetime(2026, 5, 20, 9, 0))
        TimeClockShift.objects.create(
            user=self.staff, clock_in_at=ci, clock_out_at=ci + timedelta(hours=8)
        )
        self.client.force_login(self.super)
        resp = self.client.get(reverse("timeclock_export_csv"), {"range": "30"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp["Content-Type"])
        text = resp.content.decode("utf-8")
        self.assertIn("staff,date,clock_in,clock_out,gross_hours,break_hours,net_hours,open,auto_closed", text)


class AdminTimeClockServiceTests(TestCase):
    """Superuser punch/add/edit/delete services with overlap + bounds validation."""

    def setUp(self):
        self.user = User.objects.create_user(username="emp", is_staff=True)
        self.admin = User.objects.create_user(username="boss2", is_staff=True, is_superuser=True)
        self.base = _aware(datetime(2026, 6, 1, 9, 0))

    def test_create_shift_overlap_rejected(self):
        admin_create_shift(self.user, self.base, self.base + timedelta(hours=8), self.admin)
        with self.assertRaises(TimeClockError):
            admin_create_shift(
                self.user, self.base + timedelta(hours=1), self.base + timedelta(hours=2), self.admin
            )
        nxt = self.base + timedelta(days=1)
        s = admin_create_shift(self.user, nxt, nxt + timedelta(hours=4), self.admin)
        self.assertEqual(s.edited_by, self.admin)

    def test_create_shift_end_before_start_rejected(self):
        with self.assertRaises(TimeClockError):
            admin_create_shift(self.user, self.base, self.base - timedelta(hours=1), self.admin)

    def test_update_shift_self_ok_sibling_overlap_rejected(self):
        s1 = admin_create_shift(self.user, self.base, self.base + timedelta(hours=4), self.admin)
        s2 = admin_create_shift(
            self.user, self.base + timedelta(hours=5), self.base + timedelta(hours=8), self.admin
        )
        # extend s1 within itself — fine
        admin_update_shift(s1, self.base, self.base + timedelta(hours=4, minutes=30), self.admin, note="ok")
        s1.refresh_from_db()
        self.assertEqual(s1.note, "ok")
        # edit s2 to overlap s1 — rejected
        with self.assertRaises(TimeClockError):
            admin_update_shift(s2, self.base + timedelta(hours=1), self.base + timedelta(hours=6), self.admin)

    def test_delete_shift_cascades_break(self):
        s = admin_create_shift(self.user, self.base, self.base + timedelta(hours=8), self.admin)
        admin_add_break(s, self.base + timedelta(hours=2), self.base + timedelta(hours=2, minutes=30), self.admin)
        admin_delete_shift(s)
        self.assertFalse(TimeClockShift.objects.filter(pk=s.pk).exists())
        self.assertEqual(TimeClockBreak.objects.count(), 0)

    def test_punch_in_stamps_editor_not_autoclosed(self):
        s = admin_punch_in(self.user, self.admin, now=self.base)
        self.assertEqual(s.edited_by, self.admin)
        self.assertFalse(s.auto_closed)
        self.assertTrue(s.is_open)

    def test_punch_in_backdated(self):
        earlier = self.base - timedelta(hours=1)
        s = admin_punch_in(self.user, self.admin, now=self.base, at=earlier)
        self.assertEqual(s.clock_in_at, earlier)
        self.assertTrue(s.is_open)

    def test_punch_in_future_rejected(self):
        with self.assertRaises(TimeClockError):
            admin_punch_in(self.user, self.admin, now=self.base, at=self.base + timedelta(hours=1))

    def test_punch_out_backdated(self):
        admin_punch_in(self.user, self.admin, now=self.base - timedelta(hours=3), at=self.base - timedelta(hours=3))
        s = admin_punch_out(self.user, self.admin, now=self.base, at=self.base - timedelta(hours=1))
        self.assertEqual(s.clock_out_at, self.base - timedelta(hours=1))
        self.assertFalse(s.is_open)

    def test_break_within_bounds_and_overlap(self):
        s = admin_create_shift(self.user, self.base, self.base + timedelta(hours=8), self.admin)
        with self.assertRaises(TimeClockError):  # outside the shift
            admin_add_break(s, self.base + timedelta(hours=9), self.base + timedelta(hours=9, minutes=30), self.admin)
        admin_add_break(s, self.base + timedelta(hours=2), self.base + timedelta(hours=2, minutes=30), self.admin)
        with self.assertRaises(TimeClockError):  # overlaps the first break
            admin_add_break(s, self.base + timedelta(hours=2, minutes=15), self.base + timedelta(hours=3), self.admin)


class AdminTimeClockViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="s2", password="x", is_staff=True)
        self.super = User.objects.create_user(username="b2", password="x", is_staff=True, is_superuser=True)

    def test_manage_requires_superuser(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("timeclock_manage")).status_code, 302)

    def test_manage_and_detail_load_for_superuser(self):
        self.client.force_login(self.super)
        self.assertEqual(self.client.get(reverse("timeclock_manage")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("timeclock_staff_detail", args=[self.staff.id])).status_code, 200
        )

    def test_entry_action_add_and_delete(self):
        self.client.force_login(self.super)
        ci = _aware(datetime(2026, 6, 1, 9, 0))
        co = _aware(datetime(2026, 6, 1, 17, 0))
        resp = self.client.post(
            reverse("timeclock_entry_action"),
            data=json.dumps({
                "action": "add_shift", "user_id": self.staff.id,
                "clock_in_at": timezone.localtime(ci).strftime("%Y-%m-%dT%H:%M"),
                "clock_out_at": timezone.localtime(co).strftime("%Y-%m-%dT%H:%M"),
            }),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["success"])
        shift = TimeClockShift.objects.get(user=self.staff)
        resp2 = self.client.post(
            reverse("timeclock_entry_action"),
            data=json.dumps({"action": "delete_shift", "shift_id": shift.id}),
            content_type="application/json",
        )
        self.assertTrue(resp2.json()["success"])
        self.assertFalse(TimeClockShift.objects.filter(pk=shift.pk).exists())

    def test_schedule_save_get_and_override(self):
        self.client.force_login(self.super)
        save = self.client.post(
            reverse("staff_schedule_action"),
            data=json.dumps({
                "action": "save_weekly", "user_id": self.staff.id,
                "weekly": {"0": {"is_working": True, "start_time": "09:00", "end_time": "17:00", "note": ""}},
            }),
            content_type="application/json",
        )
        self.assertTrue(save.json()["success"])
        self.assertTrue(StaffWeeklySchedule.objects.filter(user=self.staff, day_of_week=0).exists())

        got = self.client.get(reverse("staff_schedule_get"), {"user_id": self.staff.id})
        self.assertTrue(got.json()["success"])
        self.assertIn("0", got.json()["weekly"])

        add = self.client.post(
            reverse("staff_schedule_action"),
            data=json.dumps({"action": "add_override", "user_id": self.staff.id, "date": "2026-06-10", "kind": "off"}),
            content_type="application/json",
        )
        self.assertTrue(add.json()["success"])
        oid = StaffScheduleOverride.objects.get(user=self.staff).id
        delete = self.client.post(
            reverse("staff_schedule_action"),
            data=json.dumps({"action": "delete_override", "user_id": self.staff.id, "id": oid}),
            content_type="application/json",
        )
        self.assertTrue(delete.json()["success"])
        self.assertFalse(StaffScheduleOverride.objects.filter(pk=oid).exists())
