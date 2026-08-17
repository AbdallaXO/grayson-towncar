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
from ops.models import StaffWeeklySchedule, StaffScheduleOverride, StaffOnCall, StaffExtraShift
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


class CoveringShiftTests(TestCase):
    """A one-off shift on a day someone doesn't normally work — the cover case."""

    def setUp(self):
        self.jo = _staff("jo", "Joseph")
        _weekly(self.jo, 0, time(9), time(17))            # Mondays only
        _weekly(self.jo, 4, None, None, is_working=False)  # explicitly off Fridays
        self.friday = MONDAY + timedelta(days=4)

    def _cover(self, start=time(9), end=time(17), end_date=None):
        return StaffScheduleOverride.objects.create(
            user=self.jo, date=self.friday, end_date=end_date, kind="custom_hours",
            start_time=start, end_time=end, status="approved")

    def test_one_off_puts_them_on_a_day_they_are_normally_off(self):
        self._cover()
        day = coverage.dated_range([self.friday], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(day["on_count"], 1)
        self.assertEqual(day["lanes"][0][0]["name"], "Joseph")

    def test_it_is_marked_covering_not_merely_changed(self):
        """The board has to say 'covering' — a favour reads differently to a tweak."""
        self._cover()
        bar = coverage.dated_range([self.friday], _roster(), today=MONDAY)["weekdays"][0]["lanes"][0][0]
        self.assertTrue(bar["covering"])
        self.assertFalse(bar["changed"])

    def test_changed_hours_on_a_usual_day_is_not_covering(self):
        StaffScheduleOverride.objects.create(
            user=self.jo, date=MONDAY, kind="custom_hours",
            start_time=time(11), end_time=time(15), status="approved")
        bar = coverage.dated_range([MONDAY], _roster(), today=MONDAY)["weekdays"][0]["lanes"][0][0]
        self.assertTrue(bar["changed"])
        self.assertFalse(bar["covering"])

    def test_the_recurring_pattern_is_untouched(self):
        """The whole point: covering one Friday must not add every Friday."""
        self._cover()
        pattern = coverage.weekly_pattern(_proster(), today_dow=0)
        friday_cell = pattern["rows"][0]["cells"][4]
        self.assertFalse(friday_cell["is_working"])
        self.assertEqual(friday_cell["label"], "Off")
        self.assertEqual(pattern["weekdays"][4]["on_count"], 0)

    def test_a_multi_day_cover_spans_the_range(self):
        self._cover(end_date=self.friday + timedelta(days=1))
        days = coverage.dated_range([self.friday, self.friday + timedelta(days=1)],
                                    _roster(), today=MONDAY)["weekdays"]
        self.assertEqual([d["on_count"] for d in days], [1, 1])
        self.assertTrue(all(d["lanes"][0][0]["covering"] for d in days))

    def test_cell_carries_the_override_id_so_it_can_be_removed(self):
        ov = self._cover()
        cell = coverage.dated_range([self.friday], _roster(), today=MONDAY)["rows"][0]["cells"][0]
        self.assertEqual(cell["ov_id"], ov.id)

    def test_typical_hours_prefill(self):
        self.assertEqual(coverage.typical_hours(self.jo), {"start": "09:00", "end": "17:00"})
        blank = _staff("blank", "Blank")
        self.assertEqual(coverage.typical_hours(blank), {"start": "09:00", "end": "17:00"})
        picky = _staff("picky", "Picky")
        _weekly(picky, 0, time(6, 30), time(15))
        _weekly(picky, 1, time(6, 30), time(15))
        _weekly(picky, 2, time(11), time(19))
        self.assertEqual(coverage.typical_hours(picky), {"start": "06:30", "end": "15:00"})


class AddShiftEndpointTests(TestCase):
    def setUp(self):
        self.url = reverse("staffing_action")
        self.jo = _staff("jo", "Joseph")
        _weekly(self.jo, 0, time(9), time(17))
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        self.client.force_login(self.boss)
        today = timezone.localdate()
        self.friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def _add(self, **kw):
        body = {"action": "add_shift", "user_id": self.jo.id,
                "date": self.friday.strftime("%Y-%m-%d"), "start": "09:00", "end": "20:00"}
        body.update(kw)
        return self._post(body)

    def test_adds_a_single_day_cover(self):
        self.assertTrue(self._add(note="Covering for Luis").json()["success"])
        ov = StaffScheduleOverride.objects.get(user=self.jo, date=self.friday)
        self.assertEqual((ov.kind, ov.status, ov.end_date), ("custom_hours", "approved", None))
        self.assertEqual(ov.note, "Covering for Luis")
        # And the recurring pattern gained nothing.
        self.assertFalse(StaffWeeklySchedule.objects.filter(user=self.jo, day_of_week=4).exists())

    def test_adds_a_multi_day_cover(self):
        through = self.friday + timedelta(days=1)
        self._add(through=through.strftime("%Y-%m-%d"))
        ov = StaffScheduleOverride.objects.get(user=self.jo, date=self.friday)
        self.assertEqual(ov.end_date, through)

    def test_role_can_be_set_at_the_same_time(self):
        self._add(role="closer")
        self.assertEqual(StaffScheduleOverride.objects.get(user=self.jo, date=self.friday).role, "closer")

    def test_missing_times_rejected(self):
        self.assertFalse(self._add(start="", end="").json()["success"])

    def test_identical_times_rejected(self):
        self.assertFalse(self._add(start="09:00", end="09:00").json()["success"])

    def test_backwards_range_rejected(self):
        past = self.friday - timedelta(days=2)
        self.assertFalse(self._add(through=past.strftime("%Y-%m-%d")).json()["success"])

    def test_cannot_schedule_someone_who_is_booked_off(self):
        timeoff.submit_request(self.jo, self.friday, by=self.boss, approved=True)
        body = self._add().json()
        self.assertFalse(body["success"])
        self.assertIn("booked off", body["error"])

    def test_a_clash_anywhere_in_the_range_is_caught(self):
        timeoff.submit_request(self.jo, self.friday + timedelta(days=1), by=self.boss, approved=True)
        body = self._add(through=(self.friday + timedelta(days=2)).strftime("%Y-%m-%d")).json()
        self.assertFalse(body["success"])

    def test_re_adding_the_same_date_updates_rather_than_duplicates(self):
        self._add(start="09:00", end="17:00")
        self._add(start="10:00", end="18:00")
        rows = StaffScheduleOverride.objects.filter(user=self.jo, date=self.friday)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().start_time, time(10))

    def test_remove_shift(self):
        self._add()
        ov = StaffScheduleOverride.objects.get(user=self.jo, date=self.friday)
        self.assertTrue(self._post({"action": "remove_shift", "id": ov.id}).json()["success"])
        self.assertFalse(StaffScheduleOverride.objects.filter(pk=ov.id).exists())

    def test_remove_shift_refuses_a_time_off_row(self):
        ov = timeoff.submit_request(self.jo, self.friday, by=self.boss, approved=True)
        self.assertEqual(self._post({"action": "remove_shift", "id": ov.id}).status_code, 404)
        self.assertTrue(StaffScheduleOverride.objects.filter(pk=ov.id).exists())

    def test_board_offers_add_on_an_empty_day(self):
        html = self.client.get(reverse("staffing_board"), {"scope": "week"}).content.decode()
        self.assertIn('class="sp-addbtn"', html)
        self.assertIn("+ shift", html)

    def test_pattern_scope_offers_no_dated_add(self):
        """The recurring week is the wrong place to add a one-off.

        Asserted on the button's class, not the data attribute — the attribute
        also appears in the page's own JS selector, so it is always present.
        """
        html = self.client.get(reverse("staffing_board")).content.decode()
        self.assertNotIn('class="sp-addbtn"', html)

    def test_pattern_scope_points_at_the_week_view_instead(self):
        """An inert cell is what sends someone hunting through the admin."""
        html = self.client.get(reverse("staffing_board")).content.decode()
        self.assertIn("sp-addlink", html)
        self.assertIn("+ on a date", html)
        self.assertIn("scope=week", html)

    def test_dated_scope_tells_you_empty_days_are_clickable(self):
        html = self.client.get(reverse("staffing_board"), {"scope": "week"}).content.decode()
        self.assertIn("empty day to add a one-off shift", html)

    def test_covering_shift_renders_its_badge_and_remove_hook(self):
        self._add()
        html = self.client.get(reverse("staffing_board"), {"scope": "week"}).content.decode()
        self.assertIn("covering", html)
        self.assertIn("data-cover-id", html)


class SplitShiftTests(TestCase):
    """Morning shift, long gap, evening shift — two windows on one day."""

    def setUp(self):
        self.iris = _staff("iris", "Iris")
        _weekly(self.iris, 2, time(9), time(13))          # Wednesday morning
        self.wed = MONDAY + timedelta(days=2)

    def _extra(self, start=time(17), end=time(21), recurring=True, role=""):
        return StaffExtraShift.objects.create(
            user=self.iris, day_of_week=2 if recurring else None,
            date=None if recurring else self.wed,
            start_time=start, end_time=end, role=role)

    def test_recurring_split_shows_both_windows_on_the_pattern(self):
        self._extra()
        day = coverage.weekly_pattern(_proster(), today_dow=2)["weekdays"][2]
        labels = sorted(b["label"] for lane in day["lanes"] for b in lane)
        self.assertEqual(labels, ["5p–9p", "9a–1p"])

    def test_one_off_split_shows_on_that_date_only(self):
        self._extra(recurring=False)
        wed = coverage.dated_range([self.wed], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(len([b for lane in wed["lanes"] for b in lane]), 2)
        nxt = coverage.dated_range([self.wed + timedelta(days=7)], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(len([b for lane in nxt["lanes"] for b in lane]), 1)

    def test_the_gap_between_halves_is_real_uncovered_time(self):
        """1 PM–5 PM has nobody on — the board must not paper over it."""
        self._extra()
        day = coverage.weekly_pattern(_proster(), today_dow=2)["weekdays"][2]
        self.assertEqual(day["cue"]["level"], "crit")
        self.assertTrue(any(g["left"] <= _pct(13 * 60) and g["width"] > 0 for g in day["rail_gaps"]))

    def test_cell_stacks_the_second_window(self):
        self._extra()
        cell = coverage.weekly_pattern(_proster(), today_dow=2)["rows"][0]["cells"][2]
        self.assertEqual(cell["label"], "9a–1p")
        self.assertEqual([x["label"] for x in cell["extras"]], ["5p–9p"])

    def test_hours_count_both_halves(self):
        self._extra()
        row = coverage.weekly_pattern(_proster(), today_dow=2)["rows"][0]
        self.assertEqual(row["hours"], "8h")          # 4h morning + 4h evening

    def test_opener_and_closer_land_on_the_right_half(self):
        """The same person holds both windows; only the morning one opens."""
        self._extra()
        day = coverage.weekly_pattern(_proster(), today_dow=2)["weekdays"][2]
        bars = sorted((b for lane in day["lanes"] for b in lane), key=lambda b: b["left"])
        self.assertEqual((bars[0]["is_opener"], bars[0]["is_closer"]), (True, False))
        self.assertEqual((bars[1]["is_opener"], bars[1]["is_closer"]), (False, True))

    def test_approved_time_off_clears_the_whole_day(self):
        """A recurring evening half must not survive a day off."""
        self._extra()
        StaffScheduleOverride.objects.create(
            user=self.iris, date=self.wed, kind="off", status="approved", reason="pto")
        day = coverage.dated_range([self.wed], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(day["on_count"], 0)
        self.assertEqual([t["name"] for t in day["time_off"]], ["Iris"])

    def test_pending_time_off_leaves_the_split_intact(self):
        self._extra()
        StaffScheduleOverride.objects.create(
            user=self.iris, date=self.wed, kind="off", status="pending", requested_by_staff=True)
        day = coverage.dated_range([self.wed], _roster(), today=MONDAY)["weekdays"][0]
        self.assertEqual(day["on_count"], 2)

    def test_model_refuses_both_or_neither_of_date_and_weekday(self):
        from django.core.exceptions import ValidationError
        both = StaffExtraShift(user=self.iris, day_of_week=2, date=self.wed,
                               start_time=time(17), end_time=time(21))
        with self.assertRaises(ValidationError):
            both.clean()
        neither = StaffExtraShift(user=self.iris, start_time=time(17), end_time=time(21))
        with self.assertRaises(ValidationError):
            neither.clean()

    def test_scheduled_minutes_sum_both_halves(self):
        """Otherwise an 8h split day reads 'Short' against a 4h primary window."""
        self._extra()
        u = User.objects.prefetch_related(
            "weekly_schedule_rows", "schedule_overrides", "extra_shifts").get(pk=self.iris.pk)
        vs = scheduling.schedule_vs_actual(u, self.wed, shifts=[], now=timezone.now())
        self.assertEqual(vs["scheduled_minutes"], 480)
        self.assertTrue(vs["is_split"])
        # Same clock format the single-window label uses (drivers.fmt_time_long,
        # which drops a whole-hour ":00") — just both halves, joined.
        self.assertEqual(vs["scheduled_label"], "9 AM – 1 PM + 5 PM – 9 PM")

    def test_my_schedule_label_names_both_halves(self):
        self._extra()
        roster = list(office_staff_qs().prefetch_related("weekly_schedule_rows", "extra_shifts"))
        wed = coverage.my_week(self.iris, roster, today_dow=2)["days"][2]
        self.assertEqual(wed["label"], "9:00 AM – 1:00 PM + 5:00 PM – 9:00 PM")
        self.assertEqual(len(wed["roster_on"]), 2)   # both halves listed


def _pct(minutes):
    return round(minutes / 1440 * 100, 3)


class SplitShiftEndpointTests(TestCase):
    def setUp(self):
        self.url = reverse("staffing_action")
        self.iris = _staff("iris", "Iris")
        _weekly(self.iris, 2, time(9), time(13))
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        self.client.force_login(self.boss)
        today = timezone.localdate()
        self.wed = today + timedelta(days=(2 - today.weekday()) % 7 or 7)

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_adding_to_a_day_they_already_work_makes_a_second_shift(self):
        resp = self._post({"action": "add_shift", "user_id": self.iris.id,
                           "date": self.wed.strftime("%Y-%m-%d"), "start": "17:00", "end": "21:00"})
        self.assertTrue(resp.json()["as_extra"])
        self.assertEqual(StaffExtraShift.objects.filter(user=self.iris).count(), 1)
        # ...and the morning window is untouched.
        self.assertEqual(StaffWeeklySchedule.objects.get(user=self.iris, day_of_week=2).start_time, time(9))
        self.assertFalse(StaffScheduleOverride.objects.filter(user=self.iris).exists())

    def test_explicit_as_extra_on_an_empty_day(self):
        thu = self.wed + timedelta(days=1)
        self._post({"action": "add_shift", "user_id": self.iris.id, "as_extra": True,
                    "date": thu.strftime("%Y-%m-%d"), "start": "17:00", "end": "21:00"})
        self.assertEqual(StaffExtraShift.objects.filter(user=self.iris, date=thu).count(), 1)

    def test_multi_day_split_creates_one_row_per_day(self):
        self._post({"action": "add_shift", "user_id": self.iris.id, "as_extra": True,
                    "date": self.wed.strftime("%Y-%m-%d"),
                    "through": (self.wed + timedelta(days=2)).strftime("%Y-%m-%d"),
                    "start": "17:00", "end": "21:00"})
        self.assertEqual(StaffExtraShift.objects.filter(user=self.iris).count(), 3)

    def test_recurring_split(self):
        resp = self._post({"action": "add_recurring_extra", "user_id": self.iris.id,
                           "dow": 2, "start": "17:00", "end": "21:00", "role": "closer"})
        self.assertTrue(resp.json()["success"])
        x = StaffExtraShift.objects.get(user=self.iris)
        self.assertEqual((x.day_of_week, x.date, x.role), (2, None, "closer"))

    def test_recurring_split_rejects_a_bad_weekday(self):
        self.assertFalse(self._post({"action": "add_recurring_extra", "user_id": self.iris.id,
                                     "dow": 9, "start": "17:00", "end": "21:00"}).status_code == 200)

    def test_remove_a_split_half(self):
        x = StaffExtraShift.objects.create(user=self.iris, day_of_week=2,
                                           start_time=time(17), end_time=time(21))
        self.assertTrue(self._post({"action": "remove_shift", "extra_id": x.id}).json()["success"])
        self.assertFalse(StaffExtraShift.objects.filter(pk=x.id).exists())

    def test_board_renders_the_second_chip_and_its_controls(self):
        StaffExtraShift.objects.create(user=self.iris, day_of_week=2,
                                       start_time=time(17), end_time=time(21))
        html = self.client.get(reverse("staffing_board")).content.decode()
        self.assertIn("sp-chip sp-split", html)
        self.assertIn("data-extra-id", html)


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
