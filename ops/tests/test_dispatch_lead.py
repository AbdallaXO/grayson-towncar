"""The Dispatch Lead group — a team lead between dispatcher and admin.

Founder decision 2026-10-02: the lead sees the team (who is on the clock now,
the staffing board) but only looks — hour totals, the payroll export and every
schedule edit stay with an admin. The lead can also hand a shift note to
someone else, which had a permission and an endpoint but no button.

The group itself is created by ops migration 0027, so every test database has it.
"""

import json
from datetime import datetime, time, timedelta

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ops import shift_services, team_today, timeoff
from ops.models import (
    OperationalTask,
    ShiftChecklist,
    ShiftException,
    StaffActivity,
    StaffExtraShift,
    StaffWeeklySchedule,
    TimeClockBreak,
    TimeClockShift,
)

LEAD_GROUP = "Dispatch Lead"


def _staff(username, first=""):
    return User.objects.create_user(username=username, password="x", is_staff=True, first_name=first)


def _lead(username="lena", first="Lena"):
    user = _staff(username, first)
    user.groups.add(Group.objects.get(name=LEAD_GROUP))
    return User.objects.get(pk=user.pk)  # fresh permission cache


class DispatchLeadGroupTests(TestCase):
    def test_the_group_carries_exactly_the_lead_permissions(self):
        group = Group.objects.get(name=LEAD_GROUP)
        self.assertEqual(
            set(group.permissions.values_list("content_type__app_label", "codename")),
            {
                ("reservations", "approve_refund"),
                ("reservations", "remove_keoi"),
                ("ops", "view_team"),
                ("ops", "review_checklists"),
                ("ops", "reopen_checklist"),
                ("ops", "edit_exception_owner"),
            },
        )

    def test_a_member_is_a_lead_and_nothing_more(self):
        lead = _lead()
        self.assertTrue(lead.has_perm("reservations.approve_refund"))
        self.assertTrue(lead.has_perm("ops.view_team"))
        self.assertFalse(lead.is_superuser)
        # No model permissions: those would open editing screens in the Django admin.
        self.assertFalse(lead.has_perm("reservations.change_reservation"))
        self.assertFalse(lead.has_perm("ops.change_timeclockshift"))


class WhosOnNowTests(TestCase):
    def setUp(self):
        self.lead = _lead()
        self.iris = _staff("iris", "Iris")
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        TimeClockShift.objects.create(user=self.iris, clock_in_at=timezone.now() - timedelta(hours=2))

    def test_lead_sees_who_is_on_the_clock_but_no_hour_totals(self):
        self.client.force_login(self.lead)
        r = self.client.get(reverse("timeclock_overview"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["show_hours"])
        self.assertContains(r, """<i class="bi bi-broadcast me-2"></i>Who's on now</h1>""")
        self.assertContains(r, "Iris")
        self.assertNotContains(r, "Hours by staff")
        self.assertNotContains(r, reverse("timeclock_export_csv"))
        self.assertNotIn("rows", r.context)

    def test_lead_cannot_pull_the_payroll_export(self):
        self.client.force_login(self.lead)
        r = self.client.get(reverse("timeclock_export_csv"))
        self.assertEqual(r.status_code, 302)

    def test_admin_still_gets_the_hours_report(self):
        self.client.force_login(self.boss)
        r = self.client.get(reverse("timeclock_overview"))
        self.assertTrue(r.context["show_hours"])
        self.assertContains(r, "Hours by staff")

    def test_plain_dispatcher_is_turned_away(self):
        self.client.force_login(self.iris)
        r = self.client.get(reverse("timeclock_overview"))
        self.assertEqual(r.status_code, 302)


class LookOnlyStaffingBoardTests(TestCase):
    def setUp(self):
        self.lead = _lead()
        self.jo = _staff("jo", "Joseph")
        for dow in range(7):
            StaffWeeklySchedule.objects.create(
                user=self.jo, day_of_week=dow, is_working=True,
                start_time=time(9), end_time=time(17),
            )
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")
        today = timezone.localdate()
        self.friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        timeoff.submit_request(self.jo, self.friday, reason="vacation", note="family wedding")
        # Already booked off, a week later: the lead sees who and when, not the note.
        timeoff.submit_request(self.jo, self.friday + timedelta(days=7), reason="personal",
                               note="hospital visit", by=self.boss, approved=True)

    def test_lead_sees_the_board_with_no_controls(self):
        self.client.force_login(self.lead)
        r = self.client.get(reverse("staffing_board") + "?scope=week")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["can_edit"])
        self.assertContains(r, "Joseph")
        self.assertNotContains(r, "data-action-url")
        self.assertNotContains(r, 'data-decide="approve"')
        self.assertNotContains(r, 'class="sp-addbtn" data-add-shift')
        self.assertNotContains(r, '<button type="button" class="sp-rolebtn"')
        self.assertContains(r, '<span class="sp-rolebtn"')  # same chips, as information
        self.assertNotContains(r, reverse("timeclock_staff_detail", args=[self.jo.id]))
        self.assertNotContains(r, 'id="spRolePop"')

    def test_lead_does_not_see_pending_requests_or_their_notes(self):
        self.client.force_login(self.lead)
        r = self.client.get(reverse("staffing_board") + "?scope=week")
        self.assertEqual(r.context["pending_timeoff"], [])
        self.assertNotContains(r, "Time off requested")
        self.assertNotContains(r, "family wedding")

    def test_lead_sees_who_is_booked_off_but_not_the_note(self):
        self.client.force_login(self.lead)
        r = self.client.get(reverse("staffing_board") + "?scope=week")
        self.assertTrue(r.context["upcoming_timeoff"])
        self.assertContains(r, "Booked off")
        self.assertNotContains(r, "hospital visit")

    def test_lead_cannot_edit_through_the_endpoint(self):
        self.client.force_login(self.lead)
        r = self.client.post(
            reverse("staffing_action"),
            data=json.dumps({"action": "add_shift", "user_id": self.jo.id,
                             "date": self.friday.strftime("%Y-%m-%d"), "start": "18:00", "end": "22:00"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(StaffExtraShift.objects.filter(user=self.jo).exists())

    def test_admin_keeps_the_full_board(self):
        self.client.force_login(self.boss)
        r = self.client.get(reverse("staffing_board") + "?scope=week")
        self.assertTrue(r.context["can_edit"])
        self.assertContains(r, "data-action-url")
        self.assertContains(r, 'data-decide="approve"')
        self.assertContains(r, "hospital visit")

    def test_plain_dispatcher_is_turned_away(self):
        self.client.force_login(self.jo)
        r = self.client.get(reverse("staffing_board"))
        self.assertEqual(r.status_code, 302)


class LeadShiftMenuTests(TestCase):
    def test_lead_finds_the_team_pages_in_the_shift_menu(self):
        self.client.force_login(_lead())
        r = self.client.get(reverse("timeclock"))
        self.assertContains(r, "Who's on now")
        self.assertContains(r, "Staffing board")
        self.assertContains(r, "How shifts ran")

    def test_plain_dispatcher_does_not(self):
        self.client.force_login(_staff("plain", "Plain"))
        r = self.client.get(reverse("timeclock"))
        self.assertNotContains(r, "Who's on now")
        self.assertNotContains(r, "Staffing board")

    def test_admin_keeps_their_own_links_and_gets_no_duplicates(self):
        self.client.force_login(User.objects.create_superuser("boss", "boss@x.com", "pw"))
        r = self.client.get(reverse("timeclock_overview"))
        self.assertNotContains(r, "Who's on now</a>")
        self.assertContains(r, reverse("staffing_board"))


class ShiftNoteHandoffTests(TestCase):
    def setUp(self):
        self.lead = _lead()
        self.iris = _staff("iris", "Iris")
        self.luis = _staff("luis", "Luis")
        self.day = timezone.localdate()
        self.checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.iris,
        )
        self.note = shift_services.document_exception(
            self.checklist,
            what="One trip still needs a chauffeur.",
            owner=self.iris,
            next_action="Call Carlos at 9 AM.",
            user=self.iris,
            row_key="unassigned",
        )

    def _page(self):
        return self.client.get(reverse("shift_open") + f"?date={self.day:%Y-%m-%d}")

    def _hand_off(self, owner):
        return self.client.post(
            reverse("shift_action"),
            data=json.dumps({"action": "reassign", "checklist_id": self.checklist.id,
                             "exception_id": self.note.id, "owner_id": owner.id}),
            content_type="application/json",
        )

    def test_lead_gets_the_hand_off_button(self):
        self.client.force_login(self.lead)
        r = self._page()
        self.assertContains(r, f'data-handoff-for="{self.note.id}"')
        self.assertContains(r, f'data-handoff-form="{self.note.id}"')

    def test_plain_dispatcher_does_not(self):
        self.client.force_login(self.iris)
        r = self._page()
        self.assertContains(r, "One trip still needs a chauffeur.")
        self.assertNotContains(r, f'data-handoff-for="{self.note.id}"')
        self.assertNotContains(r, f'data-handoff-form="{self.note.id}"')

    def test_lead_hands_the_note_to_someone_else(self):
        self.client.force_login(self.lead)
        r = self._hand_off(self.luis)
        self.assertTrue(r.json()["success"], r.content)
        self.assertEqual(ShiftException.objects.get(pk=self.note.pk).owner, self.luis)

    def test_plain_dispatcher_cannot(self):
        self.client.force_login(self.iris)
        r = self._hand_off(self.luis)
        self.assertFalse(r.json()["success"])
        self.assertEqual(ShiftException.objects.get(pk=self.note.pk).owner, self.iris)


class TeamTodayBuildTests(TestCase):
    """The Team page's data, at a pinned 3 PM so no test depends on the hour it runs."""

    def setUp(self):
        self.today = timezone.localdate()
        tz = timezone.get_current_timezone()
        self.now = timezone.make_aware(datetime.combine(self.today, time(15, 0)), tz)
        self.at = lambda h, m=0: timezone.make_aware(datetime.combine(self.today, time(h, m)), tz)
        dow = self.today.weekday()

        self.iris = _staff("iris", "Iris")      # working
        self.obed = _staff("obed", "Obed")      # on a break
        self.zoe = _staff("zoe", "Zoe")         # down from 9, never came in
        self.tad = _staff("tad", "Tadashi")     # on later
        self.bryan = _staff("bryan", "Bryan")   # came and went
        self.jo = _staff("jo", "Joseph")        # off
        self.boss = User.objects.create_superuser("boss", "boss@x.com", "pw")

        for user, start, end in [(self.iris, 9, 17), (self.zoe, 9, 17), (self.tad, 18, 23), (self.bryan, 6, 11)]:
            StaffWeeklySchedule.objects.create(
                user=user, day_of_week=dow, is_working=True, start_time=time(start), end_time=time(end),
            )
        TimeClockShift.objects.create(user=self.iris, clock_in_at=self.at(9, 2))
        on_break = TimeClockShift.objects.create(user=self.obed, clock_in_at=self.at(12))
        TimeClockBreak.objects.create(shift=on_break, break_start_at=self.at(14, 50))
        TimeClockShift.objects.create(user=self.bryan, clock_in_at=self.at(6), clock_out_at=self.at(11))
        # Zoe has used the clock before, so not coming in today is a no-show, not "untracked".
        last_week = self.at(9) - timedelta(days=7)
        TimeClockShift.objects.create(user=self.zoe, clock_in_at=last_week, clock_out_at=last_week + timedelta(hours=8))

        # Iris's plate and her day.
        OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="Call the Smiths about the return",
            assigned_to=self.iris, due_at=self.at(14),
        )
        done = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="Confirm the 4:30 Disney pickup",
            assigned_to=self.iris, due_at=self.at(10),
        )
        OperationalTask.objects.filter(pk=done.pk).update(
            status=OperationalTask.Status.COMPLETED, resolved_by=self.iris, resolved_at=self.at(11),
        )
        OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="Nobody has this one", due_at=self.at(13),
        )
        StaffActivity.objects.filter(pk=StaffActivity.objects.create(
            user=self.iris, action_type=StaffActivity.ActionType.PAGE_VIEW, path="/dispatching/",
        ).pk).update(created_at=self.at(14, 45))

        # The boss was busy in the app today but is never "on" the team page for it.
        StaffActivity.objects.create(user=self.boss, action_type=StaffActivity.ActionType.PAGE_VIEW, path="/dispatching/")

    def _by_name(self, out):
        return {p["name"]: p for p in out["people"]}

    def test_every_dispatcher_on_today_gets_a_line_saying_where_they_are(self):
        people = self._by_name(team_today.build(now=self.now))
        self.assertEqual(people["Iris"]["state"], "working")
        self.assertEqual(people["Iris"]["label"], "Working since 9:02 AM")
        self.assertEqual(people["Obed"]["state"], "on_break")
        self.assertEqual(people["Obed"]["label"], "On a break since 2:50 PM")
        self.assertEqual(people["Zoe"]["state"], "no_show")
        self.assertEqual(people["Zoe"]["label"], "Hasn't clocked in — was due 9 AM")
        self.assertEqual(people["Tadashi"]["label"], "Starts 6 PM")
        self.assertEqual(people["Bryan"]["label"], "Clocked out 11:00 AM")

    def test_whoever_needs_a_look_comes_first(self):
        names = [p["name"] for p in team_today.build(now=self.now)["people"]]
        self.assertEqual(names[0], "Zoe")

    def test_everyone_else_is_listed_as_off_and_the_boss_is_neither(self):
        out = team_today.build(now=self.now)
        self.assertIn("Joseph", [o["name"] for o in out["off"]])
        self.assertNotIn("boss", [p["name"] for p in out["people"]])
        self.assertNotIn("boss", [o["name"] for o in out["off"]])

    def test_a_line_shows_the_plate_and_the_day(self):
        iris = self._by_name(team_today.build(now=self.now))["Iris"]
        self.assertEqual(iris["tasks_open"], 1)
        self.assertEqual(iris["tasks_overdue"], 1)
        self.assertEqual(iris["tasks_shown"][0]["title"], "Call the Smiths about the return")
        self.assertEqual(iris["did"]["tasks_done"], 1)
        self.assertEqual(iris["last_did"], "Opened the dashboard")
        self.assertEqual(iris["last_did_at"], "2:45 PM")

    def test_what_needs_a_decision(self):
        out = team_today.build(now=self.now)
        self.assertEqual(out["unclaimed_count"], 1)
        self.assertEqual(out["unclaimed_overdue"], 1)
        self.assertEqual(out["checklist_open"]["text"], "Not started")

    def test_refunds_only_for_someone_who_can_approve_them(self):
        self.assertNotIn("refunds", team_today.build(now=self.now))
        self.assertEqual(team_today.build(now=self.now, include_refunds=True)["refund_count"], 0)


class TeamTodayPageTests(TestCase):
    def test_lead_opens_the_team_page_with_refunds(self):
        self.client.force_login(_lead())
        r = self.client.get(reverse("team_today"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Team today")
        self.assertContains(r, "Refunds waiting")

    def test_team_access_alone_shows_no_refunds(self):
        user = _staff("viewer", "Vera")
        user.user_permissions.add(Permission.objects.get(codename="view_team"))
        self.client.force_login(User.objects.get(pk=user.pk))
        r = self.client.get(reverse("team_today"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Refunds waiting")

    def test_plain_dispatcher_is_turned_away(self):
        self.client.force_login(_staff("plain", "Plain"))
        r = self.client.get(reverse("team_today"))
        self.assertEqual(r.status_code, 302)

    def test_the_founders_get_it_too_with_the_link_and_the_refunds(self):
        self.client.force_login(User.objects.create_superuser("boss", "boss@x.com", "pw"))
        r = self.client.get(reverse("team_today"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'href="{reverse("team_today")}"')
        self.assertContains(r, "Refunds waiting")
        self.assertContains(r, "Activity today")

    def test_the_team_link_is_in_the_top_bar_for_the_lead_only(self):
        link = f'href="{reverse("team_today")}"'
        self.client.force_login(_lead())
        self.assertContains(self.client.get(reverse("timeclock")), link)
        self.client.force_login(_staff("plain", "Plain"))
        self.assertNotContains(self.client.get(reverse("timeclock")), link)


class ActivityTodayTests(TestCase):
    """The simple line per person: busy or idle right now, booked, tasks done, last thing done."""

    def setUp(self):
        from reservations.models import AuditLog

        self.AuditLog = AuditLog
        self.today = timezone.localdate()
        tz = timezone.get_current_timezone()
        self.now = timezone.make_aware(datetime.combine(self.today, time(15, 0)), tz)
        self.at = lambda h, m=0: timezone.make_aware(datetime.combine(self.today, time(h, m)), tz)
        dow = self.today.weekday()

        self.iris = _staff("iris", "Iris")      # clocked in, did something 15 minutes ago
        self.andres = _staff("andres", "Andres")  # clocked in, last did something at 1:30
        self.obed = _staff("obed", "Obed")      # on a break
        for user in (self.iris, self.andres, self.obed):
            StaffWeeklySchedule.objects.create(
                user=user, day_of_week=dow, is_working=True, start_time=time(9), end_time=time(17),
            )
        TimeClockShift.objects.create(user=self.iris, clock_in_at=self.at(9))
        TimeClockShift.objects.create(user=self.andres, clock_in_at=self.at(12))
        on_break = TimeClockShift.objects.create(user=self.obed, clock_in_at=self.at(9))
        TimeClockBreak.objects.create(shift=on_break, break_start_at=self.at(14, 50))

        self._page_view(self.iris, self.at(14, 45))
        self._change(self.andres, "driver_assigned", "Leg", self.at(13, 30))

    def _page_view(self, user, when, path="/dispatching/"):
        sa = StaffActivity.objects.create(user=user, action_type=StaffActivity.ActionType.PAGE_VIEW, path=path)
        StaffActivity.objects.filter(pk=sa.pk).update(created_at=when)

    def _change(self, user, action, model_name, when):
        row = self.AuditLog.objects.create(
            model_name=model_name, object_id=1, action=action, user=user, username=user.username,
        )
        self.AuditLog.objects.filter(pk=row.pk).update(timestamp=when)

    def _people(self):
        return {p["name"]: p for p in team_today.build(now=self.now)["people"]}

    def test_someone_who_just_did_something_is_busy(self):
        iris = self._people()["Iris"]
        self.assertEqual((iris["now_words"], iris["now_tone"]), ("Busy", "ok"))

    def test_clocked_in_with_nothing_done_for_half_an_hour_is_idle(self):
        andres = self._people()["Andres"]
        self.assertEqual((andres["now_words"], andres["now_tone"]), ("Idle · 1h 30m", "warn"))
        self.assertEqual(andres["last_did"], "Assigned a driver")
        self.assertEqual(andres["last_did_at"], "1:30 PM")

    def test_idle_counts_from_clock_in_when_they_have_done_nothing_at_all(self):
        zoe = _staff("zoe", "Zoe")
        StaffWeeklySchedule.objects.create(
            user=zoe, day_of_week=self.today.weekday(), is_working=True, start_time=time(14), end_time=time(22),
        )
        TimeClockShift.objects.create(user=zoe, clock_in_at=self.at(14, 10))
        zoe_row = self._people()["Zoe"]
        self.assertEqual(zoe_row["now_words"], "Idle · 50 min")
        self.assertEqual(zoe_row["last_did"], "")

    def test_someone_who_never_came_in_says_when_they_were_due(self):
        zoe = _staff("zoe", "Zoe")
        StaffWeeklySchedule.objects.create(
            user=zoe, day_of_week=self.today.weekday(), is_working=True, start_time=time(9), end_time=time(17),
        )
        last_week = self.at(9) - timedelta(days=7)
        TimeClockShift.objects.create(user=zoe, clock_in_at=last_week, clock_out_at=last_week + timedelta(hours=8))
        zoe_row = self._people()["Zoe"]
        self.assertEqual((zoe_row["now_words"], zoe_row["now_tone"]), ("Not in · due 9 AM", "bad"))

    def test_a_break_says_how_long(self):
        obed = self._people()["Obed"]
        self.assertEqual((obed["now_words"], obed["now_tone"]), ("On a break · 10 min", "info"))

    def test_the_latest_of_either_log_is_the_last_thing_they_did(self):
        self._change(self.iris, "created", "Reservation", self.at(14, 55))
        iris = self._people()["Iris"]
        self.assertEqual(iris["last_did"], "Booked a reservation")
        self.assertEqual(iris["last_did_at"], "2:55 PM")

    def test_team_totals_and_the_busy_idle_count(self):
        done = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="Confirm the 4:30", due_at=self.at(10),
        )
        OperationalTask.objects.filter(pk=done.pk).update(
            status=OperationalTask.Status.COMPLETED, resolved_by=self.andres, resolved_at=self.at(11),
        )
        out = team_today.build(now=self.now)
        self.assertEqual(out["team_tasks_done"], 1)
        self.assertEqual(out["team_booked"], 0)
        self.assertEqual(out["busy_count"], 1)
        self.assertEqual(out["idle_count"], 1)

    def test_the_page_shows_the_activity_lines(self):
        self.client.force_login(_lead())
        r = self.client.get(reverse("team_today"))
        self.assertContains(r, "Activity today")
        self.assertContains(r, "Last thing they did")
        self.assertContains(r, "they may be on the phone")
