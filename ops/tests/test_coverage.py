"""Tests for the cross-dispatcher coverage aggregation (ops/coverage.py).

Covers the interval sweep, tiered time-of-day targets, on-call as an overnight
body, the handoff-sliver filter, overnight cross-midnight, and the board view.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ops import coverage
from ops.models import StaffWeeklySchedule, StaffScheduleOverride, StaffOnCall
from ops.staff import office_staff_qs


MONDAY = datetime(2026, 6, 1).date()       # weekday 0
SATURDAY = MONDAY + timedelta(days=5)      # weekday 5 (weekend, core target 1)


def _staff(name, first=""):
    return User.objects.create_user(username=name, first_name=first, is_staff=True)


def _weekly(user, dow, start, end, is_working=True):
    return StaffWeeklySchedule.objects.create(
        user=user, day_of_week=dow, is_working=is_working, start_time=start, end_time=end,
    )


def _oncall(user, date, start=time(0), end=time(6)):
    return StaffOnCall.objects.create(user=user, date=date, start_time=start, end_time=end)


def _roster():
    return list(office_staff_qs().prefetch_related("weekly_schedule_rows", "schedule_overrides"))


class TargetTests(TestCase):
    def test_tiered_targets(self):
        self.assertEqual(coverage.target_at(time(3), 0), 1)    # overnight
        self.assertEqual(coverage.target_at(time(7), 0), 1)    # early / opening
        self.assertEqual(coverage.target_at(time(12), 0), 2)   # weekday core
        self.assertEqual(coverage.target_at(time(12), 5), 1)   # weekend core
        self.assertEqual(coverage.target_at(time(20), 0), 1)   # 8 PM = edge again
        self.assertEqual(coverage.target_at(time(23), 0), 1)   # evening


class DayCoverageTests(TestCase):
    def test_empty_day_is_critical(self):
        _staff("a")  # on roster, nothing scheduled
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["on_count"], 0)
        self.assertEqual(day["peak"], 0)
        self.assertIsNone(day["coverage_span"])
        self.assertEqual(day["risk"], "critical")
        self.assertEqual(day["worst_issue"]["level"], "crit")

    def test_full_day_weekend_is_covered(self):
        # Weekend core target = 1, so blanketing 24h with 1 body reads covered.
        p2 = _staff("day", "Day"); _weekly(p2, 5, time(6), time(16))
        p3 = _staff("eve", "Eve"); _weekly(p3, 5, time(16), time(0))   # 4 PM → midnight
        oncaller = _staff("oc", "Ona"); _oncall(oncaller, SATURDAY)     # 12–6 AM
        day = coverage.day_coverage(SATURDAY, _roster(), today=SATURDAY)
        self.assertEqual(day["risk"], "covered")
        self.assertEqual(day["worst_issue"]["level"], "ok")
        self.assertEqual(day["on_count"], 2)          # p2, p3 start today; on-caller isn't a "worker"
        self.assertEqual(len(day["oncall"]), 1)
        self.assertEqual(day["oncall"][0]["name"], "Ona")

    def test_core_hole_is_critical(self):
        _oncall(_staff("oc"), MONDAY)
        _weekly(_staff("am"), 0, time(6), time(9))
        _weekly(_staff("eve"), 0, time(20), time(0))   # 8 PM → midnight
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["risk"], "critical")
        self.assertEqual(day["worst_issue"]["level"], "crit")
        self.assertIn("No coverage", day["worst_issue"]["text"])

    def test_one_of_two_in_core_is_understaffed(self):
        _oncall(_staff("oc"), MONDAY)                  # overnight covered
        _weekly(_staff("solo", "Solo"), 0, time(6), time(0))  # 6 AM → midnight, one body
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(day["risk"], "understaffed")  # core wants 2, has 1
        self.assertEqual(day["worst_issue"]["level"], "soft")
        self.assertEqual(day["on_count"], 1)
        self.assertEqual(len(day["oncall"]), 1)

    def test_overnight_only_uncovered_is_soft_not_critical(self):
        # Weekend: core covered by one body, but 12–6 AM has no on-call.
        _weekly(_staff("d", "Dee"), 5, time(6), time(0))  # 6 AM → midnight
        day = coverage.day_coverage(SATURDAY, _roster(), today=SATURDAY)
        self.assertEqual(day["risk"], "understaffed")     # overnight gap = amber, not red
        self.assertEqual(day["worst_issue"]["level"], "soft")

    def test_oncall_closes_the_overnight_gap(self):
        d = _staff("d", "Dee"); _weekly(d, 5, time(6), time(0))
        _oncall(_staff("oc", "Ona"), SATURDAY)
        day = coverage.day_coverage(SATURDAY, _roster(), today=SATURDAY)
        self.assertEqual(day["risk"], "covered")          # on-call filled 12–6

    def test_handoff_sliver_is_ignored(self):
        # On-call ends 6:00, opener arrives 6:30 → 30-min gap must NOT flag.
        _oncall(_staff("oc"), SATURDAY)
        _weekly(_staff("d"), 5, time(6, 30), time(0))
        day = coverage.day_coverage(SATURDAY, _roster(), today=SATURDAY)
        self.assertEqual(day["risk"], "covered")


class OvernightTests(TestCase):
    def test_overnight_flagged_and_tail_counts_next_day(self):
        u = _staff("night", "Nadia")
        _weekly(u, 0, time(20), time(2))   # Mon 8 PM → Tue 2 AM
        mon = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertIn("Nadia", mon["overnight"])

        tue = coverage.day_coverage(MONDAY + timedelta(days=1), _roster(), today=MONDAY)
        self.assertEqual(tue["on_count"], 0)   # nobody starts Tuesday
        self.assertEqual(tue["peak"], 1)       # but the 12–2 AM tail covers it


class WeekCoverageTests(TestCase):
    def test_shape_grid_and_oncall_marker(self):
        u = _staff("wk", "Wanda")
        _weekly(u, 0, time(9), time(17))            # working Monday
        _weekly(u, 1, None, None, is_working=False) # off Tuesday
        _oncall(u, MONDAY + timedelta(days=1))      # on-call Tuesday
        data = coverage.week_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertEqual(len(data["days"]), 7)
        row = data["rows"][0]
        self.assertEqual(len(row["cells"]), 7)
        self.assertTrue(row["cells"][0]["is_working"])
        self.assertTrue(row["cells"][0]["is_today"])
        self.assertTrue(row["cells"][1]["is_oncall"])      # Tuesday on-call marker
        self.assertEqual(row["oncall_days"], 1)
        self.assertEqual(row["cells"][2]["label"], "—")    # no schedule Wednesday

    def test_grid_cell_overnight_flag(self):
        u = _staff("n2")
        _weekly(u, 0, time(22), time(6))
        data = coverage.week_coverage(MONDAY, _roster(), today=MONDAY)
        self.assertTrue(data["rows"][0]["cells"][0]["is_overnight"])


class DstTests(TestCase):
    def test_spring_forward_day_does_not_crash(self):
        dst_sunday = datetime(2026, 3, 8).date()   # US/Eastern spring-forward
        self.assertEqual(dst_sunday.weekday(), 6)
        _weekly(_staff("dst"), 6, time(0), time(6))
        day = coverage.day_coverage(dst_sunday, _roster(), today=dst_sunday)
        self.assertGreaterEqual(day["peak"], 1)


class StaffingBoardViewTests(TestCase):
    def setUp(self):
        self.url = reverse("staffing_board")
        u = _staff("boardstaff", "Board")
        _weekly(u, 0, time(9), time(17))
        _oncall(u, MONDAY)

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
        self.client.force_login(User.objects.create_user("plainstaff", is_staff=True, password="pw"))
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
