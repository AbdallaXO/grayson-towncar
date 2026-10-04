"""Weekly-schedule save — only what the caller sent is written.

Two screens post to ``save_driver_weekly_schedules``: the planner's "Driver
Schedules" modal (Off / Flex / Start / End / Preference per day) and the
schedule page's "Edit Schedules" drawer. Neither screen shows every column, and
the endpoint used to fill each missing key with a hard-coded fallback — so a
modal save quietly wiped max hours, preferred shift, scheduling notes and the
named shift type on all seven days, and every drawer save reset the driver's
defaults to 6 AM–11 PM flexible.

What must hold:
  * PARTIAL: a key that isn't in the payload is never written — not on the day
    rows, not on the driver's defaults. Future columns (the structured-shift
    fields) are therefore never touched by this endpoint either.
  * A day with no row yet is seeded from the driver's defaults, so a missing
    key keeps meaning what the board was already showing.
  * Ticking Flex on (without naming a shift type) still opens the day up — the
    board only reads a day as flexible when it is also full-day.
  * ALL OR NOTHING: hours outside 0–23 (and other junk) are refused with a 400
    and nothing from the request is saved.
  * The drawer no longer posts hard-coded driver defaults.

Run with:
  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_weekly_schedule_save
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from drivers.models import Driver, DriverWeeklySchedule

MON, TUE, SAT = 0, 1, 5


class _ScheduleSaveFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("ws_staff", password="x", is_staff=True)
        cls.george = Driver.objects.create(
            profile=User.objects.create_user("ws_george", first_name="George"),
            driver_type="inhouse",
            default_start_hour=5,
            default_end_hour=17,
            default_flexible=False,
            default_shift_type="morning",
            default_max_hours=Decimal("9.0"),
            default_preferred_shift="morning",
            default_preference="prefer_arrival",
        )
        cls.sam = Driver.objects.create(
            profile=User.objects.create_user("ws_sam", first_name="Sam"),
            driver_type="inhouse",
        )
        # Every column the modal does NOT show is set to something non-default
        # so a fallback overwrite is visible.
        for day in range(7):
            DriverWeeklySchedule.objects.create(
                driver=cls.george, day_of_week=day,
                is_available=True, shift_type="morning",
                start_hour=5, end_hour=13, flexible=False,
                max_hours=Decimal("8.0"), preferred_shift="morning",
                preference="prefer_arrival",
                scheduling_notes="Leaves by 1 for school pickup",
            )

    def setUp(self):
        self.client.force_login(self.staff)

    def _post(self, drivers):
        return self.client.post(
            reverse("save_driver_weekly_schedules"),
            data=json.dumps({"drivers": drivers}),
            content_type="application/json",
        )

    def _modal_payload(self, driver, weekly):
        """Exactly what the planner modal's collectSchedules() sends: four
        echoed defaults plus five keys per day."""
        return {
            "id": driver.id,
            "default_start_hour": driver.default_start_hour,
            "default_end_hour": driver.default_end_hour,
            "default_flexible": driver.default_flexible,
            "default_preference": driver.default_preference,
            "weekly": weekly,
        }

    @staticmethod
    def _modal_day(is_available=True, start_hour=5, end_hour=13,
                   flexible=False, preference="prefer_arrival"):
        return {"is_available": is_available, "start_hour": start_hour,
                "end_hour": end_hour, "flexible": flexible,
                "preference": preference}


class ModalSaveLeavesUnsentColumnsAlone(_ScheduleSaveFixture):
    def test_modal_save_keeps_max_hours_preferred_shift_notes_and_shift_type(self):
        weekly = {str(d): self._modal_day() for d in range(7)}
        weekly[str(TUE)] = self._modal_day(start_hour=7, end_hour=15)

        resp = self._post([self._modal_payload(self.george, weekly)])

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["success"])
        for row in DriverWeeklySchedule.objects.filter(driver=self.george):
            with self.subTest(day=row.day_of_week):
                self.assertEqual(row.shift_type, "morning")
                self.assertEqual(row.max_hours, Decimal("8.0"))
                self.assertEqual(row.preferred_shift, "morning")
                self.assertEqual(row.scheduling_notes, "Leaves by 1 for school pickup")
        # ...while what the modal does edit is written.
        tue = DriverWeeklySchedule.objects.get(driver=self.george, day_of_week=TUE)
        self.assertEqual((tue.start_hour, tue.end_hour), (7, 15))

    def test_modal_save_keeps_every_driver_default(self):
        resp = self._post([self._modal_payload(
            self.george, {str(MON): self._modal_day()})])

        self.assertEqual(resp.status_code, 200, resp.content)
        g = Driver.objects.get(pk=self.george.pk)
        self.assertEqual(g.default_start_hour, 5)
        self.assertEqual(g.default_end_hour, 17)
        self.assertFalse(g.default_flexible)
        self.assertEqual(g.default_shift_type, "morning")
        self.assertEqual(g.default_max_hours, Decimal("9.0"))
        self.assertEqual(g.default_preferred_shift, "morning")
        self.assertEqual(g.default_preference, "prefer_arrival")

    def test_save_only_writes_the_columns_it_was_sent(self):
        # Pins partial semantics at the SQL level, so columns added later
        # (shift_template / shift_start / shift_end) are never clobbered.
        with CaptureQueriesContext(connection) as ctx:
            resp = self._post([self._modal_payload(
                self.george, {str(MON): self._modal_day(start_hour=6)})])
        self.assertEqual(resp.status_code, 200, resp.content)

        updates = [q["sql"] for q in ctx.captured_queries
                   if q["sql"].startswith("UPDATE")]
        day_updates = [s for s in updates if "drivers_driverweeklyschedule" in s]
        driver_updates = [s for s in updates if '"drivers_driver"' in s]
        self.assertTrue(day_updates)
        for sql in day_updates:
            for column in ("shift_type", "max_hours", "preferred_shift",
                           "scheduling_notes"):
                self.assertNotIn(f'"{column}"', sql)
        for sql in driver_updates:
            for column in ("default_shift_type", "default_max_hours",
                           "default_preferred_shift", "notes"):
                self.assertNotIn(f'"{column}"', sql)

    def test_payload_without_weekly_or_defaults_changes_nothing_but_notes(self):
        resp = self._post([{"id": self.george.id, "notes": "  Prefers the S-Class  "}])

        self.assertEqual(resp.status_code, 200, resp.content)
        g = Driver.objects.get(pk=self.george.pk)
        self.assertEqual(g.notes, "Prefers the S-Class")
        self.assertEqual(g.default_start_hour, 5)
        self.assertEqual(g.default_shift_type, "morning")
        mon = DriverWeeklySchedule.objects.get(driver=self.george, day_of_week=MON)
        self.assertEqual((mon.start_hour, mon.end_hour, mon.flexible), (5, 13, False))


class ExplicitKeysStillWrite(_ScheduleSaveFixture):
    def test_drawer_shaped_day_writes_everything_it_sends(self):
        resp = self._post([{"id": self.george.id, "weekly": {str(MON): {
            "is_available": True, "shift_type": "custom", "start_hour": 8,
            "end_hour": 18, "flexible": False, "max_hours": None,
            "preferred_shift": "", "preference": "",
            "scheduling_notes": "",
        }}}])

        self.assertEqual(resp.status_code, 200, resp.content)
        mon = DriverWeeklySchedule.objects.get(driver=self.george, day_of_week=MON)
        self.assertEqual(mon.shift_type, "custom")
        self.assertEqual((mon.start_hour, mon.end_hour), (8, 18))
        self.assertIsNone(mon.max_hours)          # an explicit null clears it
        self.assertEqual(mon.preferred_shift, "")
        self.assertEqual(mon.scheduling_notes, "")

    def test_explicit_driver_default_is_written(self):
        resp = self._post([{"id": self.george.id, "default_max_hours": 10,
                            "default_shift_type": "full_day"}])

        self.assertEqual(resp.status_code, 200, resp.content)
        g = Driver.objects.get(pk=self.george.pk)
        self.assertEqual(g.default_max_hours, Decimal("10.0"))
        self.assertEqual(g.default_shift_type, "full_day")
        self.assertEqual(g.default_start_hour, 5)   # not sent, not touched


class NewDayRowsAndFlex(_ScheduleSaveFixture):
    def test_new_day_row_is_seeded_from_driver_defaults(self):
        DriverWeeklySchedule.objects.filter(driver=self.george, day_of_week=SAT).delete()

        resp = self._post([self._modal_payload(self.george, {
            str(SAT): self._modal_day(start_hour=5, end_hour=17)})])

        self.assertEqual(resp.status_code, 200, resp.content)
        sat = DriverWeeklySchedule.objects.get(driver=self.george, day_of_week=SAT)
        self.assertEqual(sat.shift_type, "morning")
        self.assertEqual(sat.max_hours, Decimal("9.0"))
        self.assertEqual(sat.preferred_shift, "morning")
        self.assertEqual((sat.start_hour, sat.end_hour), (5, 17))

    def test_ticking_flex_on_a_fixed_day_makes_it_an_open_day(self):
        DriverWeeklySchedule.objects.filter(driver=self.george, day_of_week=MON).update(
            shift_type="custom", flexible=False)

        resp = self._post([self._modal_payload(self.george, {
            str(MON): self._modal_day(flexible=True)})])

        self.assertEqual(resp.status_code, 200, resp.content)
        mon = DriverWeeklySchedule.objects.get(driver=self.george, day_of_week=MON)
        self.assertTrue(mon.flexible)
        self.assertEqual(mon.shift_type, "full_day")

    def test_unchanged_flex_leaves_a_named_shift_alone(self):
        DriverWeeklySchedule.objects.filter(driver=self.george, day_of_week=MON).update(
            shift_type="evening", flexible=True)

        resp = self._post([self._modal_payload(self.george, {
            str(MON): self._modal_day(flexible=True)})])

        self.assertEqual(resp.status_code, 200, resp.content)
        mon = DriverWeeklySchedule.objects.get(driver=self.george, day_of_week=MON)
        self.assertEqual(mon.shift_type, "evening")


class ValidationIsAllOrNothing(_ScheduleSaveFixture):
    def _assert_refused(self, drivers):
        resp = self._post(drivers)
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertTrue(body["error"])
        return body["error"]

    def test_hour_24_is_refused(self):
        error = self._assert_refused([self._modal_payload(
            self.george, {str(MON): self._modal_day(end_hour=24)})])
        self.assertIn("Monday", error)

    def test_negative_default_hour_is_refused(self):
        self._assert_refused([{"id": self.george.id, "default_start_hour": -1}])
        self.assertEqual(Driver.objects.get(pk=self.george.pk).default_start_hour, 5)

    def test_non_numeric_hour_is_a_400_not_a_crash(self):
        self._assert_refused([self._modal_payload(
            self.george, {str(MON): self._modal_day(start_hour="soon")})])

    def test_unknown_day_is_refused(self):
        self._assert_refused([self._modal_payload(
            self.george, {"7": self._modal_day()})])

    def test_bad_max_hours_is_refused(self):
        self._assert_refused([{"id": self.george.id,
                               "weekly": {str(MON): {"max_hours": 30}}}])

    def test_one_bad_driver_rolls_back_the_whole_save(self):
        self._assert_refused([
            {"id": self.sam.id, "notes": "Saved?",
             "weekly": {str(MON): {"start_hour": 9}}},
            self._modal_payload(self.george, {str(MON): self._modal_day(start_hour=99)}),
        ])

        sam = Driver.objects.get(pk=self.sam.pk)
        self.assertIsNone(sam.notes)
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=self.sam).exists())


class DrawerStopsSendingDefaults(_ScheduleSaveFixture):
    def test_drawer_save_keeps_defaults_preference_and_notes(self):
        # The drawer's payload after the fix: notes + the five things it edits.
        resp = self._post([{"id": self.george.id, "notes": "", "weekly": {
            str(d): {"is_available": True, "shift_type": "morning",
                     "flexible": False, "max_hours": 8, "preferred_shift": "morning",
                     "start_hour": 5, "end_hour": 13}
            for d in range(7)}}])

        self.assertEqual(resp.status_code, 200, resp.content)
        g = Driver.objects.get(pk=self.george.pk)
        self.assertEqual((g.default_start_hour, g.default_end_hour), (5, 17))
        self.assertEqual(g.default_shift_type, "morning")
        self.assertEqual(g.default_max_hours, Decimal("9.0"))
        for row in DriverWeeklySchedule.objects.filter(driver=self.george):
            with self.subTest(day=row.day_of_week):
                self.assertEqual(row.shift_type, "morning")
                self.assertEqual(row.preference, "prefer_arrival")
                self.assertEqual(row.scheduling_notes, "Leaves by 1 for school pickup")

    def test_schedule_page_does_not_post_hard_coded_driver_defaults(self):
        resp = self.client.get(reverse("inhouse_schedule"))

        self.assertEqual(resp.status_code, 200)
        page = resp.content.decode()
        self.assertIn("save_driver_weekly_schedules", page.replace("-", "_"))
        for key in ("default_start_hour", "default_end_hour", "default_flexible",
                    "default_shift_type", "default_max_hours",
                    "default_preferred_shift", "default_preference"):
            self.assertFalse(key in page, f"the drawer still posts {key}")
