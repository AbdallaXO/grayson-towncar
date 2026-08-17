"""
Tests for the expanded staffing board: assigned opener/closer roles, dated
scopes (week / day / range), per-dispatcher colours, and the time-off
request → approve workflow.

The through-line worth protecting: a *pending* request must not move anyone's
schedule, and a roster with no roles assigned must read exactly as it did before
roles existed (opener = earliest in, closer = latest out).
"""

import json
from datetime import date, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ops import coverage, scheduling, timeoff
from ops.models import StaffWeeklySchedule, StaffScheduleOverride, StaffOnCall
from ops.staff import office_staff_qs


MONDAY = date(2026, 6, 1)      # weekday 0


def _staff(username, first=""):
    return User.objects.create_user(username=username, first_name=first, is_staff=True)


def _weekly(user, dow, start, end, is_working=True, role=""):
    return StaffWeeklySchedule.objects.create(
        user=user, day_of_week=dow, is_working=is_working,
        start_time=start, end_time=end, role=role,
    )


def _roster():
    return list(office_staff_qs().prefetch_related("weekly_schedule_rows", "schedule_overrides"))


def _proster():
    return list(office_staff_qs().prefetch_related("weekly_schedule_rows"))


class RoleAssignmentTests(TestCase):
    """Assigned duty beats the hours; with nothing assigned we fall back."""

    def test_derived_when_nothing_assigned(self):
        _weekly(_staff("a", "Alice"), 0, time(6, 30), time(15))
        _weekly(_staff("b", "Bob"), 0, time(10), time(22))
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(mon["opener"]["name"], "Alice")
        self.assertEqual(mon["closer"]["name"], "Bob")
        self.assertFalse(mon["opener"]["assigned"])      # derived, not assigned
        self.assertFalse(mon["closer"]["assigned"])

    def test_assigned_opener_beats_earliest_in(self):
        _weekly(_staff("a", "Alice"), 0, time(6, 30), time(15))               # in first
        b = _staff("b", "Bob")
        _weekly(b, 0, time(10), time(23, 59), role="opener")                  # but owns opening
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(mon["opener"]["name"], "Bob")
        self.assertTrue(mon["opener"]["assigned"])
        # ...and the board says so plainly rather than colouring the day worse:
        # the coverage cue is identical with the role assignment removed.
        self.assertTrue(any("Alice is in first" in n for n in mon["role_notes"]))
        StaffWeeklySchedule.objects.filter(user=b, day_of_week=0).update(role="")
        self.assertEqual(coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]["cue"], mon["cue"])

    def test_both_role_covers_open_and_close(self):
        solo = _staff("s", "Solo")
        _weekly(solo, 0, time(8), time(20), role="both")
        _weekly(_staff("h", "Helper"), 0, time(9), time(13))
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(mon["opener"]["name"], "Solo")
        self.assertEqual(mon["closer"]["name"], "Solo")
        self.assertTrue(mon["opener"]["assigned"] and mon["closer"]["assigned"])

    def test_two_openers_flagged_but_one_wins(self):
        _weekly(_staff("a", "Alice"), 0, time(7), time(15), role="opener")
        _weekly(_staff("b", "Bob"), 0, time(9), time(17), role="opener")
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(mon["opener"]["name"], "Alice")            # earliest of the two
        self.assertTrue(any("two people assigned Opener" in n for n in mon["role_notes"]))

    def test_cells_carry_role_and_marks(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(9), time(17), role="closer")
        cell = coverage.weekly_pattern(_proster(), today_dow=0)["rows"][0]["cells"][0]
        self.assertEqual(cell["role"], "closer")
        self.assertEqual(cell["role_label"], "Closer")
        self.assertTrue(cell["is_closer"])


class ColorTests(TestCase):
    def test_each_dispatcher_gets_a_distinct_colour(self):
        for i in range(6):
            _staff(f"u{i}", f"U{i}")
        colors = coverage.assign_colors(_proster())
        inks = [c["ink"] for c in colors.values()]
        self.assertEqual(len(inks), 6)
        self.assertEqual(len(set(inks)), 6)

    def test_colour_is_stable_for_the_same_user(self):
        u = _staff("u", "U")
        first = coverage.assign_colors([u])[u.id]["ink"]
        self.assertEqual(coverage.assign_colors([u])[u.id]["ink"], first)

    def test_bars_carry_their_person_colour(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(9), time(17))
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        bar = mon["lanes"][0][0]
        self.assertIn("fill", bar["color"])
        self.assertTrue(bar["color"]["ink"].startswith("#"))


class PendingRequestIsInertTests(TestCase):
    """The core safety property: a request changes nothing until approved."""

    def setUp(self):
        self.u = _staff("u", "Uma")
        _weekly(self.u, 0, time(9), time(17))

    def _resolved(self):
        user = User.objects.prefetch_related("weekly_schedule_rows", "schedule_overrides").get(pk=self.u.pk)
        return scheduling.resolve_staff_schedule(user, MONDAY)

    def test_pending_off_does_not_change_the_schedule(self):
        StaffScheduleOverride.objects.create(
            user=self.u, date=MONDAY, kind="off", status="pending", requested_by_staff=True)
        sched = self._resolved()
        self.assertTrue(sched["is_working"])
        self.assertEqual(sched["start_time"], time(9))
        self.assertIsNone(sched["time_off"])

    def test_approved_off_does(self):
        StaffScheduleOverride.objects.create(
            user=self.u, date=MONDAY, kind="off", status="approved", reason="pto")
        sched = self._resolved()
        self.assertFalse(sched["is_working"])
        self.assertEqual(sched["time_off"]["reason_label"], "PTO / vacation")

    def test_denied_off_does_not(self):
        StaffScheduleOverride.objects.create(user=self.u, date=MONDAY, kind="off", status="denied")
        self.assertTrue(self._resolved()["is_working"])

    def test_pending_still_shows_on_the_board_as_a_request(self):
        StaffScheduleOverride.objects.create(
            user=self.u, date=MONDAY, kind="off", status="pending", requested_by_staff=True)
        data = coverage.dated_range([MONDAY], _roster(), today=MONDAY)
        day = data["weekdays"][0]
        self.assertEqual(day["on_count"], 1)          # still counted as working
        self.assertEqual(len(day["pending"]), 1)      # ...and the ask is visible
        self.assertEqual(day["time_off"], [])
        self.assertTrue(data["rows"][0]["cells"][0]["pending"])


class DatedRangeTests(TestCase):
    def test_approved_time_off_removes_the_shift(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(9), time(17))
        _weekly(_staff("c", "Cov"), 0, time(8), time(20))
        StaffScheduleOverride.objects.create(
            user=u, date=MONDAY, kind="off", status="approved", reason="sick")
        day = coverage.dated_range([MONDAY], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(day["on_count"], 1)
        self.assertEqual([t["name"] for t in day["time_off"]], ["Uma"])

    def test_custom_hours_are_marked_changed(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(9), time(17))
        StaffScheduleOverride.objects.create(
            user=u, date=MONDAY, kind="custom_hours", start_time=time(11), end_time=time(15))
        day = coverage.dated_range([MONDAY], _roster(), today=MONDAY)["weekdays"][0]
        bar = day["lanes"][0][0]
        self.assertTrue(bar["changed"])
        self.assertEqual(bar["label"], "11a–3p")

    def test_dated_role_override_wins_for_that_date_only(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(9), time(17), role="opener")
        _weekly(_staff("b", "Bo"), 0, time(7), time(16))
        StaffScheduleOverride.objects.create(user=u, date=MONDAY, kind="note", role="closer")
        day = coverage.dated_range([MONDAY], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(day["closer"]["name"], "Uma")
        self.assertTrue(day["closer"]["assigned"])
        # The recurring pattern is untouched — Uma still opens every week.
        self.assertEqual(coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]["opener"]["name"], "Uma")

    def test_actual_oncall_fills_the_night(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(6), time(23, 59))
        StaffOnCall.objects.create(user=u, date=MONDAY, start_time=time(0), end_time=time(6))
        day = coverage.dated_range([MONDAY], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(day["oncall_names"], ["Uma"])
        self.assertEqual(day["rail_gaps"], [])

    def test_oncall_carries_the_name_window_and_colour(self):
        """The board has to name who's covering the night, not just shade the band."""
        u = _staff("u", "Uma Nightingale")
        _weekly(u, 0, time(9), time(17))
        StaffOnCall.objects.create(user=u, date=MONDAY, start_time=time(0), end_time=time(5, 30))
        oc = coverage.dated_range([MONDAY], _roster(), today=MONDAY)["weekdays"][0]["oncall"]
        self.assertEqual(len(oc), 1)
        self.assertEqual(oc[0]["name"], "Uma Nightingale")
        self.assertEqual(oc[0]["short"], "Uma")
        self.assertEqual(oc[0]["window"], "12a–5:30a")
        self.assertTrue(oc[0]["color"]["ink"].startswith("#"))

    def test_pattern_scope_has_nobody_to_name(self):
        """On-call is assigned per night, so the dateless pattern names no one."""
        _weekly(_staff("u", "Uma"), 0, time(9), time(17))
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(mon["oncall"], [])

    def test_no_oncall_marked_is_not_reported_as_a_daytime_gap(self):
        # 12–6 AM is outside operating hours either way; an unmarked night must
        # not manufacture a "gap" on the rail.
        _weekly(_staff("u", "Uma"), 0, time(6), time(23, 59))
        day = coverage.dated_range([MONDAY], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(day["oncall_names"], [])
        self.assertEqual(day["rail_gaps"], [])

    def test_row_hours_total(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(9), time(17))
        _weekly(u, 1, time(9), time(17))
        dates = [MONDAY, MONDAY + timedelta(days=1)]
        row = coverage.dated_range(dates, _roster(), today=MONDAY)["rows"][0]
        self.assertEqual(row["working_days"], 2)
        self.assertEqual(row["hours"], "16h")

    def test_range_is_capped(self):
        _staff("u", "Uma")
        dates = [MONDAY + timedelta(days=i) for i in range(60)]
        data = coverage.dated_range(dates, _roster(), today=MONDAY)
        self.assertEqual(len(data["weekdays"]), coverage.MAX_RANGE_DAYS)

    def test_past_and_today_flags(self):
        _weekly(_staff("u", "Uma"), 0, time(9), time(17))
        days = coverage.dated_range([MONDAY, MONDAY + timedelta(days=1)], _roster(),
                                    today=MONDAY + timedelta(days=1))["weekdays"]
        self.assertTrue(days[0]["is_past"])
        self.assertTrue(days[1]["is_today"])


class TimeOffModuleTests(TestCase):
    def setUp(self):
        self.u = _staff("u", "Uma")
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        self.today = timezone.localdate()

    def test_staff_request_lands_pending(self):
        ov = timeoff.submit_request(self.u, self.today + timedelta(days=3), reason="pto")
        self.assertEqual(ov.status, "pending")
        self.assertTrue(ov.requested_by_staff)

    def test_manager_add_lands_approved(self):
        ov = timeoff.submit_request(self.u, self.today + timedelta(days=3), by=self.boss, approved=True)
        self.assertEqual(ov.status, "approved")
        self.assertFalse(ov.requested_by_staff)
        self.assertEqual(ov.decided_by, self.boss)

    def test_overlapping_request_rejected(self):
        start = self.today + timedelta(days=3)
        timeoff.submit_request(self.u, start, start + timedelta(days=2))
        with self.assertRaises(timeoff.TimeOffError):
            timeoff.submit_request(self.u, start + timedelta(days=1))

    def test_backwards_range_rejected(self):
        with self.assertRaises(timeoff.TimeOffError):
            timeoff.submit_request(self.u, self.today + timedelta(days=5), self.today)

    def test_overlong_request_rejected(self):
        with self.assertRaises(timeoff.TimeOffError):
            timeoff.submit_request(self.u, self.today, self.today + timedelta(days=200))

    def test_approve_then_deny_records_the_decision(self):
        ov = timeoff.submit_request(self.u, self.today + timedelta(days=3))
        timeoff.decide(ov, self.boss, approve=False, denial_reason="Too thin that week")
        ov.refresh_from_db()
        self.assertEqual(ov.status, "denied")
        self.assertEqual(ov.denial_reason, "Too thin that week")
        self.assertEqual(ov.decided_by, self.boss)
        self.assertIsNotNone(ov.decided_at)

    def test_queues_split_pending_from_approved(self):
        timeoff.submit_request(self.u, self.today + timedelta(days=3))
        timeoff.submit_request(self.u, self.today + timedelta(days=10), by=self.boss, approved=True)
        roster = _roster()
        self.assertEqual(len(timeoff.pending_requests(roster)), 1)
        self.assertEqual(len(timeoff.upcoming_approved(roster)), 1)

    def test_past_requests_drop_out_of_the_queue(self):
        StaffScheduleOverride.objects.create(
            user=self.u, date=self.today - timedelta(days=5), kind="off", status="pending")
        self.assertEqual(timeoff.pending_requests(_roster()), [])


class StaffingBoardScopeTests(TestCase):
    def setUp(self):
        self.url = reverse("staffing_board")
        self.u = _staff("u", "Uma Green")
        for d in range(5):
            _weekly(self.u, d, time(9), time(17), role="opener" if d == 0 else "")
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        self.client.force_login(self.boss)

    def test_pattern_is_the_default(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["scope"], "pattern")
        self.assertFalse(resp.context["is_dated"])
        self.assertEqual(len(resp.context["weekdays"]), 7)

    def test_week_scope_snaps_to_monday_and_labels_itself(self):
        today = timezone.localdate()
        wednesday = today - timedelta(days=today.weekday()) + timedelta(days=2)
        resp = self.client.get(self.url, {"scope": "week", "start": wednesday.strftime("%Y-%m-%d")})
        self.assertEqual(resp.context["scope"], "week")
        self.assertEqual(resp.context["weekdays"][0]["date"].weekday(), 0)
        self.assertEqual(resp.context["scope_label"], "This week")

    def test_next_week_label(self):
        today = timezone.localdate()
        nxt = today - timedelta(days=today.weekday()) + timedelta(days=7)
        resp = self.client.get(self.url, {"scope": "week", "start": nxt.strftime("%Y-%m-%d")})
        self.assertEqual(resp.context["scope_label"], "Next week")

    def test_day_scope_renders_one_day(self):
        resp = self.client.get(self.url, {"scope": "day", "date": timezone.localdate().strftime("%Y-%m-%d")})
        self.assertEqual(len(resp.context["weekdays"]), 1)
        self.assertTrue(resp.context["weekdays"][0]["is_today"])
        self.assertIn("Today", resp.context["scope_label"])

    def test_range_scope(self):
        today = timezone.localdate()
        resp = self.client.get(self.url, {
            "scope": "range", "start": today.strftime("%Y-%m-%d"),
            "end": (today + timedelta(days=9)).strftime("%Y-%m-%d"),
        })
        self.assertEqual(resp.context["scope"], "range")
        self.assertEqual(len(resp.context["weekdays"]), 10)

    def test_garbage_params_fall_back_to_a_week(self):
        resp = self.client.get(self.url, {"scope": "week", "start": "not-a-date"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["weekdays"]), 7)

    def test_toggle_and_colours_render(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="spTimeline"', html)
        self.assertIn('data-view="timeline"', html)
        self.assertIn("sp-bar", html)
        self.assertIn("--c-fill:rgba(", html)          # per-person colour reaches the bar

    def test_role_picker_is_present_on_shifts(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("data-role-btn", html)
        self.assertIn('id="spRolePop"', html)

    def test_dated_board_names_the_on_call_person(self):
        oncaller = _staff("night", "Nadia Okafor")
        StaffOnCall.objects.create(user=oncaller, date=timezone.localdate(),
                                  start_time=time(0), end_time=time(6))
        html = self.client.get(self.url, {"scope": "day",
                                         "date": timezone.localdate().strftime("%Y-%m-%d")}).content.decode()
        self.assertIn("On-call", html)
        self.assertIn("Nadia", html)          # the table's on-call row
        self.assertIn("Nadia Okafor", html)   # ...and the timeline band

    def test_unassigned_night_says_so_rather_than_going_blank(self):
        html = self.client.get(self.url, {"scope": "day",
                                         "date": timezone.localdate().strftime("%Y-%m-%d")}).content.decode()
        self.assertIn("not set", html)

    def test_time_off_cell_carries_the_styling_hook(self):
        """The cell class must match the CSS that draws the red trim."""
        timeoff.submit_request(self.u, timezone.localdate(), by=self.boss, approved=True, reason="sick")
        html = self.client.get(self.url, {"scope": "day",
                                         "date": timezone.localdate().strftime("%Y-%m-%d")}).content.decode()
        self.assertIn("sp-cell sp-timeoff", html)
        self.assertIn(".sp-cell.sp-timeoff", html)   # and the rule that styles it

    def test_csrf_token_is_on_the_page(self):
        """Every write here is a fetch(); without this the page 403s silently."""
        self.assertContains(self.client.get(self.url), "csrfmiddlewaretoken")

    def test_no_template_comment_leaks_into_the_page(self):
        """`{# #}` is single-line only — a wrapped one renders as visible text."""
        for scope in ("pattern", "week", "day"):
            html = self.client.get(self.url, {"scope": scope}).content.decode()
            self.assertNotIn("{#", html, f"stray comment in {scope} scope")
            self.assertNotIn("#}", html, f"stray comment in {scope} scope")

    def test_pending_requests_surface_on_the_board(self):
        timeoff.submit_request(self.u, timezone.localdate() + timedelta(days=4), reason="pto", note="wedding")
        resp = self.client.get(self.url)
        self.assertEqual(len(resp.context["pending_timeoff"]), 1)
        self.assertContains(resp, "Time off requested")
        self.assertContains(resp, "wedding")

    def test_non_superuser_redirected(self):
        self.client.force_login(_staff("plain", "Plain"))
        self.assertEqual(self.client.get(self.url).status_code, 302)


class StaffingActionTests(TestCase):
    def setUp(self):
        self.url = reverse("staffing_action")
        self.u = _staff("u", "Uma")
        _weekly(self.u, 0, time(9), time(17))
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        self.client.force_login(self.boss)

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def _next_monday(self):
        """A future date self.u actually works — their only shift is Monday."""
        today = timezone.localdate()
        return today + timedelta(days=7 - today.weekday())

    def test_set_recurring_role(self):
        resp = self._post({"action": "set_role", "user_id": self.u.id, "dow": 0, "role": "closer"})
        self.assertTrue(resp.json()["success"])
        self.assertEqual(StaffWeeklySchedule.objects.get(user=self.u, day_of_week=0).role, "closer")

    def test_clear_recurring_role(self):
        StaffWeeklySchedule.objects.filter(user=self.u, day_of_week=0).update(role="opener")
        self._post({"action": "set_role", "user_id": self.u.id, "dow": 0, "role": ""})
        self.assertEqual(StaffWeeklySchedule.objects.get(user=self.u, day_of_week=0).role, "")

    def test_unknown_role_rejected(self):
        resp = self._post({"action": "set_role", "user_id": self.u.id, "dow": 0, "role": "boss"})
        self.assertEqual(resp.status_code, 400)

    def test_role_on_a_day_not_worked_is_refused(self):
        resp = self._post({"action": "set_role", "user_id": self.u.id, "dow": 3, "role": "opener"})
        self.assertFalse(resp.json()["success"])

    def test_dated_role_creates_a_single_day_override(self):
        d = self._next_monday()
        resp = self._post({"action": "set_role", "user_id": self.u.id,
                           "date": d.strftime("%Y-%m-%d"), "role": "closer"})
        self.assertEqual(resp.json()["scope"], "date")
        ov = StaffScheduleOverride.objects.get(user=self.u, date=d)
        self.assertEqual((ov.kind, ov.role, ov.end_date), ("note", "closer", None))
        # ...and the recurring row is untouched.
        self.assertEqual(StaffWeeklySchedule.objects.get(user=self.u, day_of_week=0).role, "")

    def test_dated_role_refused_on_an_approved_day_off(self):
        """A note override would outrank the day off and put them back on the board."""
        d = self._next_monday()
        timeoff.submit_request(self.u, d, by=self.boss, approved=True)
        resp = self._post({"action": "set_role", "user_id": self.u.id,
                           "date": d.strftime("%Y-%m-%d"), "role": "opener"})
        self.assertFalse(resp.json()["success"])
        self.assertEqual(StaffScheduleOverride.objects.filter(user=self.u, date=d).count(), 1)
        roster = _roster()
        self.assertEqual(coverage.dated_range([d], roster, today=d)["weekdays"][0]["on_count"], 0)

    def test_clearing_a_dated_role_removes_the_empty_override(self):
        d = self._next_monday()
        self._post({"action": "set_role", "user_id": self.u.id, "date": d.strftime("%Y-%m-%d"), "role": "closer"})
        self._post({"action": "set_role", "user_id": self.u.id, "date": d.strftime("%Y-%m-%d"), "role": ""})
        self.assertFalse(StaffScheduleOverride.objects.filter(user=self.u, date=d).exists())

    def test_approve_and_deny(self):
        ov = timeoff.submit_request(self.u, timezone.localdate() + timedelta(days=3))
        self.assertTrue(self._post({"action": "approve_timeoff", "id": ov.id}).json()["success"])
        ov.refresh_from_db()
        self.assertEqual(ov.status, "approved")

        ov2 = timeoff.submit_request(self.u, timezone.localdate() + timedelta(days=20))
        self._post({"action": "deny_timeoff", "id": ov2.id, "denial_reason": "Need you that week"})
        ov2.refresh_from_db()
        self.assertEqual((ov2.status, ov2.denial_reason), ("denied", "Need you that week"))

    def test_manager_books_time_off_directly(self):
        d = timezone.localdate() + timedelta(days=5)
        resp = self._post({"action": "add_timeoff", "user_id": self.u.id,
                           "start": d.strftime("%Y-%m-%d"), "reason": "sick"})
        self.assertTrue(resp.json()["success"])
        ov = StaffScheduleOverride.objects.get(user=self.u, date=d)
        self.assertEqual((ov.status, ov.reason), ("approved", "sick"))

    def test_overlap_returns_a_message_not_a_500(self):
        d = timezone.localdate() + timedelta(days=5)
        self._post({"action": "add_timeoff", "user_id": self.u.id, "start": d.strftime("%Y-%m-%d")})
        resp = self._post({"action": "add_timeoff", "user_id": self.u.id, "start": d.strftime("%Y-%m-%d")})
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("already", body["error"])

    def test_cancel_removes_it(self):
        ov = timeoff.submit_request(self.u, timezone.localdate() + timedelta(days=5),
                                    by=self.boss, approved=True)
        self._post({"action": "cancel_timeoff", "id": ov.id})
        self.assertFalse(StaffScheduleOverride.objects.filter(pk=ov.id).exists())

    def test_non_superuser_blocked(self):
        self.client.force_login(_staff("plain", "Plain"))
        resp = self._post({"action": "set_role", "user_id": self.u.id, "dow": 0, "role": "closer"})
        self.assertEqual(resp.status_code, 302)


class MyTimeOffTests(TestCase):
    def setUp(self):
        self.url = reverse("my_timeoff_action")
        self.me = _staff("me", "Me")
        _weekly(self.me, timezone.localdate().weekday(), time(9), time(17))
        self.client.force_login(self.me)

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_dispatcher_can_request(self):
        d = timezone.localdate() + timedelta(days=6)
        resp = self._post({"action": "request", "start": d.strftime("%Y-%m-%d"),
                           "reason": "pto", "note": "family trip"})
        self.assertTrue(resp.json()["success"])
        ov = StaffScheduleOverride.objects.get(user=self.me)
        self.assertEqual((ov.status, ov.requested_by_staff, ov.note), ("pending", True, "family trip"))

    def test_request_form_and_status_render_on_my_schedule(self):
        timeoff.submit_request(self.me, timezone.localdate() + timedelta(days=6), reason="sick")
        resp = self.client.get(reverse("my_coverage"))
        self.assertContains(resp, "Request time off")
        self.assertContains(resp, "Pending review")
        self.assertContains(resp, "csrfmiddlewaretoken")

    def test_request_form_shows_even_without_a_schedule(self):
        """A new hire with no hours entered still has to be able to ask for days off."""
        newbie = _staff("newbie", "New")
        self.client.force_login(newbie)
        resp = self.client.get(reverse("my_coverage"))
        self.assertContains(resp, "No schedule set yet")
        self.assertContains(resp, "Request time off")

    def test_cannot_touch_someone_elses_request(self):
        other = _staff("other", "Other")
        ov = timeoff.submit_request(other, timezone.localdate() + timedelta(days=6))
        self.assertEqual(self._post({"action": "cancel", "id": ov.id}).status_code, 404)
        self.assertTrue(StaffScheduleOverride.objects.filter(pk=ov.id).exists())

    def test_withdraw_own_request(self):
        ov = timeoff.submit_request(self.me, timezone.localdate() + timedelta(days=6))
        self.assertTrue(self._post({"action": "cancel", "id": ov.id}).json()["success"])
        self.assertFalse(StaffScheduleOverride.objects.filter(pk=ov.id).exists())

    def test_bad_dates_return_a_message(self):
        resp = self._post({"action": "request", "start": ""})
        self.assertFalse(resp.json()["success"])

    def test_my_schedule_stays_calm(self):
        """The staff page must not inherit the admin board's risk vocabulary."""
        timeoff.submit_request(self.me, timezone.localdate() + timedelta(days=6))
        html = self.client.get(reverse("my_coverage")).content.decode().lower()
        for banned in ("understaffed", "critical", "coverage gap", "runs thin"):
            self.assertNotIn(banned, html)
