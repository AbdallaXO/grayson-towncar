"""Tests for the cross-dispatcher coverage aggregation (ops/coverage.py).

The interval sweep is the only net-new algorithm in the staffing feature, so
overlap / gap / alone / overnight / DST all get direct coverage here.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ops import coverage
from ops.models import StaffWeeklySchedule, StaffScheduleOverride
from ops.staff import office_staff_qs


MONDAY = datetime(2026, 6, 1).date()   # 2026-06-01 is a Monday (weekday 0)
SATURDAY = MONDAY + timedelta(days=5)  # weekend target = 1


def _staff(name, first=""):
    return User.objects.create_user(username=name, first_name=first, is_staff=True)


def _weekly(user, dow, start, end, is_working=True):
    return StaffWeeklySchedule.objects.create(
        user=user, day_of_week=dow, is_working=is_working,
        start_time=start, end_time=end,
    )


def _roster():
    return list(office_staff_qs().prefetch_related("weekly_schedule_rows", "schedule_overrides"))


class TargetTests(TestCase):
    def test_weekday_vs_weekend_target(self):
        self.assertEqual(coverage.target_for(MONDAY), 2)
        self.assertEqual(coverage.target_for(SATURDAY), 1)


class DayCoverageTests(TestCase):
    def test_empty_day_is_critical(self):
        _staff("a")  # on the roster but not scheduled Monday
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["on_count"], 0)
        self.assertEqual(day["peak"], 0)
        self.assertEqual(day["min_concurrent"], 0)
        self.assertEqual(day["risk"], "critical")

    def test_single_worker_is_understaffed_and_alone(self):
        u = _staff("solo", "Solo")
        _weekly(u, 0, time(9), time(17))
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["on_count"], 1)
        self.assertEqual(day["peak"], 1)
        self.assertEqual(day["min_concurrent"], 1)
        self.assertEqual(day["target"], 2)
        self.assertEqual(day["delta"], -1)
        self.assertEqual(day["risk"], "understaffed")
        self.assertFalse(day["survives_callout"])
        self.assertEqual(len(day["alone"]), 1)
        self.assertEqual(day["alone"][0]["name"], "Solo")

    def test_two_full_overlap_is_tight_at_target(self):
        _weekly(_staff("a"), 0, time(9), time(17))
        _weekly(_staff("b"), 0, time(9), time(17))
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["peak"], 2)
        self.assertEqual(day["min_concurrent"], 2)
        self.assertEqual(day["delta"], 0)
        self.assertEqual(day["risk"], "tight")          # at target, no buffer
        self.assertFalse(day["survives_callout"])
        self.assertEqual(day["alone"], [])

    def test_partial_overlap_flags_both_alone_edges(self):
        a = _staff("early", "Early")
        b = _staff("late", "Late")
        _weekly(a, 0, time(9), time(15))
        _weekly(b, 0, time(12), time(20))
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["peak"], 2)          # 12–15 both on
        self.assertEqual(day["min_concurrent"], 1)
        self.assertEqual(len(day["alone"]), 2)    # 9–12 Early, 15–20 Late
        self.assertEqual(day["opener"]["name"], "Early")
        self.assertEqual(day["closer"]["name"], "Late")

    def test_gap_between_shifts_is_critical(self):
        _weekly(_staff("am"), 0, time(6), time(12))
        _weekly(_staff("pm"), 0, time(14), time(22))
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["min_concurrent"], 0)   # 12–14 hole
        self.assertEqual(day["risk"], "critical")
        self.assertEqual(len(day["gaps"]), 1)

    def test_override_off_removes_from_coverage(self):
        u = _staff("swing")
        _weekly(u, 0, time(9), time(17))
        StaffScheduleOverride.objects.create(user=u, date=MONDAY, kind="off")
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["on_count"], 0)
        self.assertEqual(day["risk"], "critical")


class OvernightTests(TestCase):
    def test_overnight_flagged_on_start_day(self):
        u = _staff("night", "Nadia")
        _weekly(u, 0, time(20), time(2))   # Mon 8pm → Tue 2am
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["on_count"], 1)
        self.assertEqual(len(day["overnight"]), 1)
        self.assertEqual(day["overnight"][0]["name"], "Nadia")
        self.assertTrue(day["closer"]["is_overnight"])

    def test_overnight_tail_counts_toward_next_day(self):
        u = _staff("night")
        _weekly(u, 0, time(20), time(2))   # Monday overnight shift
        tuesday = MONDAY + timedelta(days=1)
        day = coverage.day_coverage(tuesday, _roster(), today=MONDAY)
        # Nobody *starts* Tuesday, but the 12am–2am tail provides coverage.
        self.assertEqual(day["on_count"], 0)
        self.assertEqual(day["peak"], 1)


class DstTests(TestCase):
    def test_spring_forward_day_does_not_crash(self):
        # 2026-03-08 is US/Eastern spring-forward (2am→3am). Endpoints exist;
        # only the interior hour is skipped, so make_aware never sees a
        # nonexistent time and the sweep still yields coverage.
        dst_sunday = datetime(2026, 3, 8).date()
        self.assertEqual(dst_sunday.weekday(), 6)
        u = _staff("dst")
        _weekly(u, 6, time(0), time(6))
        day = coverage.day_coverage(dst_sunday, _roster(), today=dst_sunday)
        self.assertEqual(day["peak"], 1)
        self.assertEqual(day["min_concurrent"], 1)


class WeekCoverageTests(TestCase):
    def test_shape_and_grid_cells(self):
        u = _staff("wk", "Wanda")
        _weekly(u, 0, time(9), time(17))          # working Monday
        _weekly(u, 1, None, None, is_working=False)  # explicit off Tuesday
        data = coverage.week_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(len(data["days"]), 7)
        self.assertEqual(len(data["dates"]), 7)
        self.assertEqual(len(data["rows"]), 1)
        row = data["rows"][0]
        self.assertEqual(len(row["cells"]), 7)
        self.assertEqual(row["name"], "Wanda")
        self.assertTrue(row["cells"][0]["is_working"])
        self.assertTrue(row["cells"][0]["is_today"])
        self.assertEqual(row["cells"][1]["label"], "Off")
        self.assertEqual(row["cells"][2]["label"], "—")   # no schedule set

    def test_grid_cell_overnight_flag(self):
        u = _staff("n2")
        _weekly(u, 0, time(22), time(6))
        data = coverage.week_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertTrue(data["rows"][0]["cells"][0]["is_overnight"])


class StaffingBoardViewTests(TestCase):
    def setUp(self):
        self.url = reverse("staffing_board")
        u = _staff("boardstaff", "Board")
        _weekly(u, 0, time(9), time(17))

    def test_superuser_renders(self):
        User.objects.create_superuser("boss", "boss@x.com", "pw")
        self.client.force_login(User.objects.get(username="boss"))
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "dispatching/staffing_board.html")
        self.assertContains(resp, "Staffing")

    def test_week_param_renders(self):
        User.objects.create_superuser("boss2", "boss2@x.com", "pw")
        self.client.force_login(User.objects.get(username="boss2"))
        resp = self.client.get(self.url, {"week": "2026-06-01"})
        self.assertEqual(resp.status_code, 200)

    def test_non_superuser_staff_blocked(self):
        staffer = User.objects.create_user("plainstaff", is_staff=True, password="pw")
        self.client.force_login(staffer)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)  # bounced by _is_superuser gate

    def test_anonymous_redirected(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
