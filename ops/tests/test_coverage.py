"""Tests for the cross-dispatcher coverage aggregation (ops/coverage.py).

Covers the interval sweep, tiered time-of-day targets, on-call as an overnight
body, the handoff-sliver filter, overnight cross-midnight, and the board view.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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


def _proster():
    return list(office_staff_qs().prefetch_related("weekly_schedule_rows"))


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


class TimelineTests(TestCase):
    def test_timeline_spans_day_and_flags_levels(self):
        _weekly(_staff("d"), 0, time(9), time(17))   # weekday, 1 body in core only
        day = coverage.day_coverage(MONDAY, _roster(), today=MONDAY)
        tl = day["timeline"]
        self.assertTrue(tl)
        self.assertAlmostEqual(sum(s["width"] for s in tl), 100.0, delta=0.5)
        levels = {s["level"] for s in tl}
        self.assertIn("gap", levels)    # nobody overnight/edges
        self.assertIn("thin", levels)   # 1 of 2 in core

    def test_oncall_band_and_fills_overnight(self):
        _weekly(_staff("d"), 5, time(6), time(0))    # weekend, 6 AM → midnight
        _oncall(_staff("oc"), SATURDAY)              # 12–6 AM
        day = coverage.day_coverage(SATURDAY, _roster(), today=SATURDAY)
        self.assertEqual(len(day["oncall_bands"]), 1)
        band = day["oncall_bands"][0]
        self.assertAlmostEqual(band["left"], 0.0, delta=0.1)     # starts 12 AM
        self.assertAlmostEqual(band["width"], 25.0, delta=0.5)   # 6h = 25% of the day
        self.assertNotIn("gap", {s["level"] for s in day["timeline"]})

    def test_board_renders_toggle(self):
        _weekly(_staff("d", "D"), 0, time(9), time(17))
        User.objects.create_superuser("boss3", "boss3@x.com", "pw")
        self.client.force_login(User.objects.get(username="boss3"))
        resp = self.client.get(reverse("staffing_board"))
        self.assertContains(resp, 'id="spTimeline"')
        self.assertContains(resp, 'data-view="timeline"')
        self.assertContains(resp, "sp-bar")


class WeeklyPatternTests(TestCase):
    def test_shape_and_cells(self):
        u = _staff("u", "Uma")
        _weekly(u, 0, time(9), time(17))              # works Monday
        _weekly(u, 1, None, None, is_working=False)   # explicit off Tuesday
        data = coverage.weekly_pattern(_proster(), today_dow=0)
        self.assertEqual(len(data["weekdays"]), 7)
        row = data["rows"][0]
        self.assertEqual(len(row["cells"]), 7)
        self.assertTrue(row["cells"][0]["is_working"])
        self.assertTrue(row["cells"][0]["is_today"])
        self.assertTrue(row["cells"][0]["is_opener"])   # only worker Monday → opener
        self.assertTrue(row["cells"][0]["is_closer"])
        self.assertEqual(row["cells"][1]["label"], "Off")
        self.assertEqual(row["cells"][2]["label"], "—")

    def test_opener_closer(self):
        a = _staff("a", "Alice"); _weekly(a, 0, time(6, 30), time(15))
        b = _staff("b", "Bob"); _weekly(b, 0, time(18, 30), time(2))   # overnight → latest out
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(mon["opener"]["name"], "Alice")
        self.assertEqual(mon["closer"]["name"], "Bob")

    def test_daytime_gap_flagged(self):
        _weekly(_staff("am"), 0, time(6), time(11))
        _weekly(_staff("pm"), 0, time(14), time(22))
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(mon["cue"]["level"], "crit")   # 11a–2p hole
        self.assertTrue(mon["rail_gaps"])

    def test_overnight_never_a_gap(self):
        # Nobody scheduled → operating hours are a gap, but the 12–6 AM on-call
        # window must never be flagged (rail gaps start at 6 AM = 25%).
        _staff("idle")
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        for g in mon["rail_gaps"]:
            self.assertGreaterEqual(g["left"], 25.0)

    def test_lane_packing(self):
        _weekly(_staff("early"), 0, time(6), time(14))
        _weekly(_staff("night"), 0, time(18, 30), time(2))   # no overlap → shares a lane
        mon = coverage.weekly_pattern(_proster(), today_dow=0)["weekdays"][0]
        self.assertEqual(len(mon["lanes"]), 1)
        self.assertEqual(len(mon["lanes"][0]), 2)


class MyWeekTests(TestCase):
    """The dispatcher's whole-week pattern view — every day, who's on + hours."""

    def _me_and_week(self, dow=0):
        me = _staff("me", "Me")
        _weekly(me, dow, time(7, 30), time(16))     # 7:30a–4p
        return me, coverage.my_week(me, _proster(), today_dow=dow)

    def test_shape_and_no_risk_fields(self):
        me, data = self._me_and_week()
        self.assertEqual(len(data["days"]), 7)
        self.assertTrue(data["has_schedule"])
        self.assertTrue(data["on_roster"])
        self.assertEqual(data["working_days"], 1)
        mon = data["days"][0]
        self.assertTrue(mon["is_working"])
        self.assertTrue(mon["is_today"])
        # Reassuring by design: none of the admin board's risk vocabulary leaks in.
        for banned in ("risk", "cue", "peak", "thin", "target", "worst_issue"):
            self.assertNotIn(banned, mon)

    def test_roster_on_lists_everyone_with_hours(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(9), time(17))
        _weekly(_staff("luis", "Luis"), 0, time(6), time(12))     # opener
        _weekly(_staff("iris", "Iris"), 0, time(14), time(22))    # closer
        mon = coverage.my_week(me, _proster(), today_dow=0)["days"][0]
        self.assertEqual([p["name"] for p in mon["roster_on"]], ["Luis", "Me", "Iris"])  # by start; own name, is_me flags it
        me_row = next(p for p in mon["roster_on"] if p["is_me"])
        self.assertEqual(me_row["window"], "9:00 AM – 5:00 PM")
        self.assertTrue(mon["roster_on"][0]["is_opener"])          # Luis opens
        self.assertTrue(mon["roster_on"][-1]["is_closer"])         # Iris closes

    def test_off_day_still_shows_who_is_working(self):
        # The whole-week ask: on a day the viewer is OFF, still show who's on.
        me = _staff("me", "Me")                                    # no Monday shift → off Monday
        _weekly(_staff("luis", "Luis"), 0, time(6), time(15))      # a coworker works Monday
        mon = coverage.my_week(me, _proster(), today_dow=0)["days"][0]
        self.assertFalse(mon["is_working"])
        self.assertEqual([p["name"] for p in mon["roster_on"]], ["Luis"])
        self.assertFalse(any(p["is_me"] for p in mon["roster_on"]))

    def test_solo_day_is_just_you(self):
        me, data = self._me_and_week()
        row = data["days"][0]["roster_on"]
        self.assertEqual(len(row), 1)
        self.assertTrue(row[0]["is_me"])

    def test_full_readable_time_format(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(9), time(17))
        mon = coverage.my_week(me, _proster(), today_dow=0)["days"][0]
        self.assertEqual(mon["label"], "9:00 AM – 5:00 PM")        # full AM/PM, always :MM
        self.assertEqual(mon["roster_on"][0]["window"], "9:00 AM – 5:00 PM")

    def test_opener_closer_marks(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(6), time(12))           # earliest in
        _weekly(_staff("late", "Late"), 0, time(10), time(22))              # latest out
        mon = coverage.my_week(me, _proster(), today_dow=0)["days"][0]
        self.assertTrue(mon["is_opener"])
        self.assertFalse(mon["is_closer"])

    def test_off_and_no_schedule_states(self):
        me = _staff("me", "Me")
        _weekly(me, 0, time(9), time(17))
        _weekly(me, 1, None, None, is_working=False)   # explicit off Tuesday
        data = coverage.my_week(me, _proster(), today_dow=0)
        self.assertEqual(data["days"][1]["label"], "Off")     # explicit off
        self.assertEqual(data["days"][2]["label"], "—")       # no row at all
        self.assertFalse(data["days"][1]["is_working"])

    def test_not_on_roster_is_calm(self):
        outsider = User.objects.create_user("ghost", is_staff=False)
        data = coverage.my_week(outsider, _proster(), today_dow=0)
        self.assertFalse(data["on_roster"])
        self.assertFalse(data["has_schedule"])
        self.assertEqual(data["working_days"], 0)

    def test_today_timeline_flags_me(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(7, 30), time(16))
        _weekly(_staff("co", "Co"), 0, time(9), time(17))
        tl = coverage.my_today_timeline(me, _proster(), today_dow=0)
        self.assertTrue(tl["i_am_working"])
        self.assertEqual(tl["on_count"], 2)
        self.assertEqual(len(tl["bars"]), 2)                 # one row per person
        self.assertEqual([b["is_me"] for b in tl["bars"]], [True, False])  # sorted by start: me 7:30 first
        self.assertEqual(len([b for b in tl["bars"] if b["is_me"]]), 1)


class DayViewActualTests(TestCase):
    """The *actual* day view: recurring pattern resolved against one-off overrides."""

    def test_story_beats(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(7, 30), time(16))
        _weekly(_staff("luis", "Luis"), 0, time(6), time(12))       # opener, leaves first
        _weekly(_staff("iris", "Iris"), 0, time(14), time(22))      # arrives after, carries on
        dv = coverage.day_view_actual(me, _roster(), MONDAY, today=MONDAY)
        self.assertEqual([b["kind"] for b in dv["beats"]], ["open", "leave", "handoff"])
        self.assertEqual((dv["beats"][0]["who"], dv["beats"][0]["time"]), ("Luis", "6:00 AM"))
        self.assertEqual(dv["beats"][1]["remaining"], ["You"])
        self.assertEqual(dv["beats"][2]["to"], [{"name": "Iris", "until": "10:00 PM"}])

    def test_names_everyone_at_a_handoff(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(9), time(17))
        _weekly(_staff("luis", "Luis"), 0, time(7, 30), time(16))
        _weekly(_staff("jo", "Joseph"), 0, time(9, 30), time(20))
        dv = coverage.day_view_actual(me, _roster(), MONDAY, today=MONDAY)
        leave = next(b for b in dv["beats"] if b["kind"] == "leave")
        self.assertEqual(set(leave["remaining"]), {"You", "Joseph"})

    def test_sick_day_drops_coworker_and_rethreads(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(9), time(17))
        _weekly(_staff("luis", "Luis"), 0, time(7, 30), time(16))    # opener, leaves before me
        jo = _staff("jo", "Joseph"); _weekly(jo, 0, time(9, 30), time(20))  # normally stays past me
        StaffScheduleOverride.objects.create(user=jo, date=MONDAY, kind="off")   # out sick this Monday
        dv = coverage.day_view_actual(me, _roster(), MONDAY, today=MONDAY)
        self.assertEqual([p["name"] for p in dv["roster_on"]], ["Luis", "Me"])  # Joseph dropped
        self.assertEqual(dv["exceptions"], [{"name": "Joseph", "kind": "off", "label": ""}])
        # With nobody after me now, the story re-threads and I'm the Closer.
        self.assertIn("close_me", [b["kind"] for b in dv["beats"]])

    def test_custom_hours_override_shows_changed(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(9), time(17))
        jo = _staff("jo", "Joseph"); _weekly(jo, 0, time(9, 30), time(20))
        StaffScheduleOverride.objects.create(user=jo, date=MONDAY, kind="custom_hours",
                                             start_time=time(11), end_time=time(15))
        dv = coverage.day_view_actual(me, _roster(), MONDAY, today=MONDAY)
        jo_row = next(p for p in dv["roster_on"] if p["name"] == "Joseph")
        self.assertEqual(jo_row["window"], "11:00 AM – 3:00 PM")
        self.assertEqual(dv["exceptions"], [{"name": "Joseph", "kind": "custom", "label": "11:00 AM – 3:00 PM"}])

    def test_my_week_actual_has_seven(self):
        me = _staff("me", "Me"); _weekly(me, 0, time(9), time(17))
        dvs = coverage.my_week_actual(me, _roster(), MONDAY, today=MONDAY)
        self.assertEqual(len(dvs), 7)
        self.assertEqual(dvs[0]["on_count"], 1)      # Monday: me
        self.assertEqual(dvs[1]["on_count"], 0)      # Tuesday: nobody


class MyCoverageViewTests(TestCase):
    def setUp(self):
        self.url = reverse("my_coverage")

    def test_plain_dispatcher_can_view_own_week(self):
        # The access change: a non-superuser staffer CAN reach this (unlike the board).
        me = _staff("disp", "Dispatch")
        me.set_password("pw"); me.save()
        _weekly(me, timezone.localdate().weekday(), time(9), time(17))
        self.client.force_login(me)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "dispatching/my_coverage.html")
        self.assertContains(resp, "My Schedule")

    def test_no_alarming_language(self):
        me = _staff("solo", "Solo")
        _weekly(me, timezone.localdate().weekday(), time(6), time(23))   # a long solo day
        self.client.force_login(me)
        html = self.client.get(self.url).content.decode().lower()
        for banned in ("understaffed", "critical", "coverage gap", "no coverage", "runs thin"):
            self.assertNotIn(banned, html)

    def test_oncall_names_who_is_on(self):
        # A teammate on-call is shown by name + window (not just a hatch band).
        dow = timezone.localdate().weekday()
        me = _staff("me", "Me"); _weekly(me, dow, time(9), time(17))
        ona = _staff("ona", "Ona Smith"); _oncall(ona, timezone.localdate())
        self.client.force_login(me)
        resp = self.client.get(self.url)
        self.assertContains(resp, "On-call tonight")
        self.assertContains(resp, "Ona Smith")        # names the on-call person
        self.assertContains(resp, "12:00 AM")          # with the full window

    def test_oncall_marks_self_as_you(self):
        me = _staff("oncaller", "Ona")
        _weekly(me, timezone.localdate().weekday(), time(9), time(17))
        _oncall(me, timezone.localdate())
        self.client.force_login(me)
        resp = self.client.get(self.url)
        self.assertContains(resp, "On-call tonight")
        self.assertContains(resp, "You")               # the viewer's own on-call reads "You"

    def test_no_schedule_empty_state(self):
        me = _staff("blank", "Blank")           # on roster, no weekly rows
        self.client.force_login(me)
        self.assertContains(self.client.get(self.url), "No schedule set yet")

    def test_day_story_renders(self):
        dow = timezone.localdate().weekday()
        me = _staff("me", "Me"); _weekly(me, dow, time(7, 30), time(16))
        luis = _staff("luis", "Luis"); _weekly(luis, dow, time(6), time(12))    # opens, hands off
        iris = _staff("iris", "Iris"); _weekly(iris, dow, time(14), time(22))   # I hand off to
        self.client.force_login(me)
        resp = self.client.get(self.url)
        self.assertContains(resp, "opens at")          # the opener beat
        self.assertContains(resp, "hands off to")      # Luis hands off while I'm on
        self.assertContains(resp, "you hand off to")   # I hand off when I leave
        self.assertContains(resp, "Luis")
        self.assertContains(resp, "Iris")

    def test_day_switch_scaffold_present(self):
        # Every weekday gets a switchable day-detail panel, and each week
        # overview card is a clickable target carrying its weekday index.
        me = _staff("me", "Me"); _weekly(me, timezone.localdate().weekday(), time(9), time(17))
        self.client.force_login(me)
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count('class="mc-dayview"'), 7)
        self.assertEqual(html.count('data-dow='), 14)     # 7 day panels + 7 week cards
        self.assertIn("tap a day above to switch", html)

    def test_week_overview_is_dated_with_toggle(self):
        # The overview carries real dates and a This week / Next week switch.
        today = timezone.localdate()
        me = _staff("me", "Me"); _weekly(me, today.weekday(), time(9), time(17))
        self.client.force_login(me)
        resp = self.client.get(self.url)
        monday = today - timedelta(days=today.weekday())
        self.assertContains(resp, coverage.md(monday))       # dated cards
        self.assertContains(resp, "Next week")
        self.assertContains(resp, "This week")
        # Next week renders the following Monday's date.
        resp2 = self.client.get(self.url + "?week=next")
        self.assertContains(resp2, coverage.md(monday + timedelta(days=7)))

    def test_week_card_shows_viewer_location(self):
        today = timezone.localdate()
        row = _weekly(_staff("wfh", "Wfh Person"), today.weekday(), time(9), time(17))
        row.location = "remote"; row.save()
        me = row.user
        self.client.force_login(me)
        html = self.client.get(self.url).content.decode()
        self.assertIn("WFH", html)
        self.assertIn("Working from home", html)

    def test_sick_day_shows_off_note_in_day_view(self):
        # Today's day-view reflects a one-off absence; the week list stays the pattern.
        today = timezone.localdate()
        dow = today.weekday()
        me = _staff("me", "Me"); _weekly(me, dow, time(9), time(17))
        jo = _staff("jo", "Joseph"); _weekly(jo, dow, time(9, 30), time(20))
        StaffScheduleOverride.objects.create(user=jo, date=today, kind="off")
        self.client.force_login(me)
        resp = self.client.get(self.url)
        self.assertContains(resp, "off today")            # calm exception note in the day-view

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)


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


class OnCallActionTests(TestCase):
    def setUp(self):
        self.url = reverse("timeclock_oncall_action")
        self.staff = _staff("dispatch1", "Dispatch")
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        self.client.force_login(self.boss)

    def _post(self, body):
        import json
        return self.client.post(self.url, data=json.dumps(body), content_type="application/json")

    def test_add_creates_oncall_with_defaults(self):
        resp = self._post({"action": "add", "user_id": self.staff.id, "date": "2026-06-01"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        oc = StaffOnCall.objects.get(user=self.staff, date=MONDAY)
        self.assertEqual((oc.start_time, oc.end_time), (time(0), time(6)))
        self.assertEqual(oc.created_by, self.boss)

    def test_add_is_idempotent_per_user_date(self):
        self._post({"action": "add", "user_id": self.staff.id, "date": "2026-06-01"})
        self._post({"action": "add", "user_id": self.staff.id, "date": "2026-06-01",
                    "start_time": "23:00", "end_time": "07:00"})
        qs = StaffOnCall.objects.filter(user=self.staff, date=MONDAY)
        self.assertEqual(qs.count(), 1)                       # update, not duplicate
        self.assertEqual(qs.first().start_time, time(23))

    def test_delete_removes(self):
        oc = _oncall(self.staff, MONDAY)
        resp = self._post({"action": "delete", "id": oc.id})
        self.assertTrue(resp.json()["success"])
        self.assertFalse(StaffOnCall.objects.filter(id=oc.id).exists())

    def test_bulk_delete_selected(self):
        a = _oncall(self.staff, MONDAY)
        b = _oncall(self.staff, MONDAY + timedelta(days=1))
        keep = _oncall(self.staff, MONDAY + timedelta(days=2))
        resp = self._post({"action": "delete", "ids": [a.id, b.id]})
        self.assertTrue(resp.json()["success"])
        self.assertEqual(resp.json()["deleted"], 2)
        self.assertEqual(list(StaffOnCall.objects.values_list("id", flat=True)), [keep.id])

    def test_bulk_delete_empty_selection_is_noop(self):
        _oncall(self.staff, MONDAY)
        resp = self._post({"action": "delete", "ids": []})
        self.assertTrue(resp.json()["success"])
        self.assertEqual(resp.json()["deleted"], 0)
        self.assertEqual(StaffOnCall.objects.count(), 1)

    def test_bulk_delete_bad_ids_rejected(self):
        _oncall(self.staff, MONDAY)
        resp = self._post({"action": "delete", "ids": ["nope"]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(StaffOnCall.objects.count(), 1)

    def test_string_ids_not_split_per_character(self):
        # "12" must be treated as a single id (12), NOT iterated into [1, 2].
        a = _oncall(self.staff, MONDAY)
        b = _oncall(self.staff, MONDAY + timedelta(days=1))
        resp = self._post({"action": "delete", "ids": "12"})  # no row 12 exists
        self.assertEqual(resp.json()["deleted"], 0)
        self.assertTrue(StaffOnCall.objects.filter(id=a.id).exists())
        self.assertTrue(StaffOnCall.objects.filter(id=b.id).exists())

    def test_manage_bulk_bar_starts_hidden(self):
        _oncall(self.staff, MONDAY)
        resp = self.client.get(reverse("timeclock_manage"))
        self.assertContains(resp, 'class="align-items-center gap-2 mb-2 d-none"')

    def test_non_superuser_blocked(self):
        self.client.force_login(_staff("plain"))
        self.assertEqual(self._post({"action": "add", "user_id": self.staff.id, "date": "2026-06-01"}).status_code, 302)
        self.assertFalse(StaffOnCall.objects.exists())

    def test_manage_page_shows_oncall_panel(self):
        _oncall(self.staff, MONDAY)
        resp = self.client.get(reverse("timeclock_manage"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "On-call")
        self.assertContains(resp, "oncallForm")

    def test_add_weekday_range_creates_each_matching_day(self):
        # Mon/Wed/Fri across one week (2026-06-01 Mon … 06-07 Sun).
        resp = self._post({"action": "add", "user_id": self.staff.id,
                           "from": "2026-06-01", "to": "2026-06-07", "weekdays": [0, 2, 4]})
        self.assertEqual(resp.json()["created"], 3)
        got = set(StaffOnCall.objects.filter(user=self.staff).values_list("date", flat=True))
        self.assertEqual(got, {MONDAY, MONDAY + timedelta(days=2), MONDAY + timedelta(days=4)})

    def test_add_weekday_repeats_across_weeks(self):
        resp = self._post({"action": "add", "user_id": self.staff.id,
                           "from": "2026-06-01", "to": "2026-06-14", "weekdays": [0]})
        self.assertEqual(resp.json()["created"], 2)  # 06-01 and 06-08
        self.assertEqual(StaffOnCall.objects.filter(user=self.staff).count(), 2)

    def test_weekday_range_without_end_defaults_one_week(self):
        resp = self._post({"action": "add", "user_id": self.staff.id,
                           "from": "2026-06-01", "weekdays": [0, 2]})  # Mon+Wed, no end
        self.assertEqual(resp.json()["created"], 2)  # 06-01 Mon, 06-03 Wed within the default week

    def test_month_long_range_in_one_submit(self):
        # Mon–Fri for all of June 2026 in a single add — no weekly repetition.
        resp = self._post({"action": "add", "user_id": self.staff.id,
                           "from": "2026-06-01", "to": "2026-06-30", "weekdays": [0, 1, 2, 3, 4]})
        self.assertTrue(resp.json()["success"])
        self.assertEqual(resp.json()["created"], 22)   # 22 weekdays in June 2026
        self.assertEqual(StaffOnCall.objects.filter(user=self.staff).count(), 22)

    def test_overlong_range_rejected(self):
        resp = self._post({"action": "add", "user_id": self.staff.id,
                           "from": "2026-01-01", "to": "2026-12-31", "weekdays": [0]})
        self.assertFalse(resp.json()["success"])
        self.assertFalse(StaffOnCall.objects.exists())
