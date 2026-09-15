"""The weekly inspection round: the reset, the suggestion, and the form.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_inspection

The two rules worth protecting are that the week resets itself on Monday with
nothing to run, and that walking the same car twice never makes two records the
count would double.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse

from dispatching import fleet_day, fleet_inspection
from dispatching.tests_fleet_desk import TODAY, _FleetFixture
from drivers.models import (
    Driver, DriverVehicleAssignment, VehicleDowntime, VehicleInspection,
    VehicleIssue,
)

# A fixed Wednesday, so "days left in the week" is never flaky.
WED = date(2026, 9, 16)
MONDAY = date(2026, 9, 14)


def _ninety_minutes(leg, target_date):
    return datetime.combine(target_date, leg.pickup_time) + timedelta(minutes=90)


class _InspectFixture(_FleetFixture):
    def week(self, day=WED, day_rows=None):
        loaded = fleet_inspection.load_week(day)
        return fleet_inspection.build_week(
            loaded, day_rows=day_rows,
            last_seen=fleet_inspection.last_seen_map(loaded["units"]))

    def inspected(self, unit, day=WED, outcome="ok", **kw):
        return VehicleInspection.objects.create(
            vehicle=unit, week_start=fleet_inspection.week_start_for(day),
            inspected_on=day, outcome=outcome, **kw)

    def tile(self, week, number):
        return next(t for t in week["tiles"] if t["number"] == number)


# ════════════════════════════════════════════════════════════════════════════
# The week, and its reset
# ════════════════════════════════════════════════════════════════════════════

class WeekTests(_InspectFixture):
    def test_the_week_starts_on_monday(self):
        for day in (MONDAY, WED, MONDAY + timedelta(days=6)):
            self.assertEqual(fleet_inspection.week_start_for(day), MONDAY, day)
        self.assertEqual(fleet_inspection.week_start_for(MONDAY + timedelta(days=7)),
                         MONDAY + timedelta(days=7))

    def test_next_monday_makes_every_car_due_again_with_nothing_to_run(self):
        """The whole reset mechanism: ask about a different week_start and the
        round is empty again. No job, no flag to clear."""
        unit = self.unit("1")
        self.inspected(unit, WED)
        self.assertEqual(self.week(WED)["done_count"], 1)
        self.assertEqual(self.tile(self.week(WED), "1")["state"], "done")

        next_week = WED + timedelta(days=7)
        self.assertEqual(self.week(next_week)["done_count"], 0)
        self.assertEqual(self.tile(self.week(next_week), "1")["state"], "due")

    def test_last_weeks_record_is_kept_not_overwritten(self):
        unit = self.unit("1")
        self.inspected(unit, WED)
        self.inspected(unit, WED + timedelta(days=7))
        self.assertEqual(VehicleInspection.objects.filter(vehicle=unit).count(), 2)

    def test_a_car_cannot_be_inspected_twice_in_one_week(self):
        from django.db import IntegrityError, transaction
        unit = self.unit("1")
        self.inspected(unit, WED)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.inspected(unit, WED + timedelta(days=1))

    def test_progress_counts_and_headline(self):
        a, b, c = self.unit("1"), self.unit("2"), self.unit("3")
        self.inspected(a, WED)
        self.inspected(b, WED, outcome="found")
        week = self.week()
        self.assertEqual((week["done_count"], week["total"], week["found_count"]), (2, 3, 1))
        self.assertEqual(week["due_count"], 1)
        self.assertIn("2 of 3 inspected", week["headline"])
        self.assertIn("1 found something", week["headline"])
        self.assertFalse(week["complete"])

    def test_a_car_in_the_shop_all_week_is_not_counted_as_missed(self):
        walkable, shopped = self.unit("1"), self.unit("2")
        VehicleDowntime.objects.create(
            vehicle=shopped, category="repair", reason="Gearbox",
            starts_on=MONDAY, expected_back_on=MONDAY + timedelta(days=10))
        self.inspected(walkable, WED)
        week = self.week()
        self.assertEqual(self.tile(week, "2")["state"], "shop")
        self.assertEqual(week["total"], 1)
        self.assertEqual(week["in_shop"], 1)
        self.assertTrue(week["complete"])

    def test_pace_says_what_it_takes_to_finish(self):
        for n in range(1, 9):
            self.unit(str(n))
        week = self.week(WED)          # Wednesday: 5 days left including today
        self.assertIn("8 to go", week["pace"])
        self.assertIn("a day", week["pace"])

    def test_a_finished_week_has_no_pace_line(self):
        self.inspected(self.unit("1"), WED)
        week = self.week()
        self.assertTrue(week["complete"])
        self.assertEqual(week["pace"], "")


# ════════════════════════════════════════════════════════════════════════════
# Which cars to walk today
# ════════════════════════════════════════════════════════════════════════════

class SuggestionTests(_InspectFixture):
    def test_it_offers_no_more_than_the_daily_target(self):
        for n in range(1, 12):
            self.unit(str(n))
        self.assertEqual(len(self.week()["suggested"]), fleet_inspection.DAILY_TARGET)

    def test_an_already_walked_car_is_never_offered_again_that_week(self):
        a, b = self.unit("1"), self.unit("2")
        self.inspected(a, WED)
        numbers = [t["number"] for t in self.week()["suggested"]]
        self.assertNotIn("1", numbers)
        self.assertIn("2", numbers)

    def test_a_car_in_the_shop_is_not_offered(self):
        self.unit("1")
        shopped = self.unit("2")
        VehicleDowntime.objects.create(
            vehicle=shopped, category="repair", reason="Gearbox",
            starts_on=MONDAY, expected_back_on=MONDAY + timedelta(days=10))
        self.assertNotIn("2", [t["number"] for t in self.week()["suggested"]])

    def test_a_car_standing_still_today_comes_first(self):
        """A car out on a run all day cannot be walked — that is the whole
        ordering rule."""
        busy, idle = self.unit("1"), self.unit("2")
        rows = [
            {"number": "1", "state": "working", "trips": 6, "note": "",
             "longest_gap": None, "usable_windows": []},
            {"number": "2", "state": "open", "trips": 0, "note": "No chauffeur",
             "longest_gap": None, "usable_windows": []},
        ]
        self.assertEqual(self.week(day_rows=rows)["suggested"][0]["number"], "2")

    def test_a_real_window_beats_a_bigger_unusable_hole(self):
        """Ranking on the raw gap would put a car with a three-hour hole behind
        an airport arrival above one with a genuinely usable two-hour window —
        and then the tile could not name a window at all."""
        big_unusable, real_window = self.unit("1"), self.unit("2")
        rows = [
            {"number": "1", "state": "working", "trips": 5, "note": "",
             "longest_gap": {"minutes": 200, "usable": False,
                             "from_label": "1:00 PM", "to_label": "4:20 PM"},
             "usable_windows": []},
            {"number": "2", "state": "working", "trips": 5, "note": "",
             "longest_gap": {"minutes": 150, "usable": True,
                             "from_label": "9:00 AM", "to_label": "11:30 AM"},
             "usable_windows": [{"minutes": 150, "usable": True,
                                 "from_label": "9:00 AM", "to_label": "11:30 AM"}]},
        ]
        week = self.week(day_rows=rows)
        self.assertEqual(week["suggested"][0]["number"], "2")
        self.assertIn("9:00 AM", self.tile(week, "2")["today"])

    def test_the_round_still_works_with_no_board_at_all(self):
        """Day Setup not run is not a reason to stop inspecting."""
        for n in range(1, 4):
            self.unit(str(n))
        week = self.week(day_rows=None)
        self.assertEqual(len(week["suggested"]), 3)

    def test_a_car_nobody_has_ever_walked_outranks_one_seen_long_ago(self):
        seen_once, never = self.unit("1"), self.unit("2")
        VehicleInspection.objects.create(
            vehicle=seen_once, week_start=MONDAY - timedelta(days=7),
            inspected_on=MONDAY - timedelta(days=7))
        numbers = [t["number"] for t in self.week()["suggested"]]
        self.assertEqual(numbers[0], "2")


# ════════════════════════════════════════════════════════════════════════════
# The checklist
# ════════════════════════════════════════════════════════════════════════════

class ChecklistTests(_FleetFixture):
    def test_every_item_key_is_unique(self):
        keys = [item["key"] for item in fleet_inspection.all_items()]
        self.assertEqual(len(keys), len(set(keys)), "duplicate checklist key")

    def test_results_from_a_browser_are_not_trusted(self):
        cleaned = fleet_inspection.clean_results({
            "oil": {"state": "ok"},
            "made_up_item": {"state": "ok"},
            "tires": {"state": "explodes"},
            "brakes": "not even a dict",
            "lights": {"state": "flag", "note": "  nearside out  "},
        })
        self.assertEqual(set(cleaned), {"oil", "lights"})
        self.assertEqual(cleaned["lights"]["note"], "nearside out")

    def test_a_note_is_capped(self):
        cleaned = fleet_inspection.clean_results({"oil": {"state": "ok", "note": "x" * 900}})
        self.assertEqual(len(cleaned["oil"]["note"]), 500)

    def test_an_item_dropped_from_the_checklist_still_reads_back(self):
        """Old records must not lose their answers when the list is edited."""
        labels = fleet_inspection.item_labels()
        self.assertIn("oil", labels)
        stored = {"oil": {"state": "ok"}, "retired_item": {"state": "flag"}}
        self.assertEqual(
            [k for k in stored if k not in labels], ["retired_item"])

    def test_form_sections_carry_saved_answers_back(self):
        inspection = VehicleInspection(
            results={"oil": {"state": "flag", "note": "a quart low"}})
        sections = fleet_inspection.form_sections(inspection)
        oil = next(i for s in sections for i in s["items"] if i["key"] == "oil")
        self.assertEqual(oil["state"], "flag")
        self.assertEqual(oil["note"], "a quart low")


# ════════════════════════════════════════════════════════════════════════════
# The pages
# ════════════════════════════════════════════════════════════════════════════

class PageTests(_InspectFixture):
    def test_the_round_renders(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_inspections"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["fleet_page"], "inspections")

    def test_the_form_renders_with_every_item(self):
        unit = self.unit("1")
        resp = self.client.get(reverse("fleet_inspect_vehicle", args=[unit.pk]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        for item in fleet_inspection.all_items():
            self.assertIn(f'name="state_{item["key"]}"', body)
            self.assertIn(f'name="photo_{item["key"]}"', body)

    def test_saving_records_the_walk(self):
        unit = self.unit("1")
        resp = self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]), {
            "state_oil": "ok", "state_tires": "ok", "note_tires": "all four fine",
            "odometer_miles": "84231", "notes": "Looks good.",
        })
        self.assertEqual(resp.status_code, 302)
        inspection = VehicleInspection.objects.get(vehicle=unit)
        self.assertEqual(inspection.outcome, VehicleInspection.OUTCOME_OK)
        self.assertEqual(inspection.odometer_miles, Decimal("84231"))
        self.assertEqual(inspection.results["tires"]["note"], "all four fine")
        self.assertEqual(inspection.week_start, fleet_inspection.week_start_for(TODAY))
        self.assertIsNone(inspection.issue)

    def test_walking_the_same_car_twice_updates_rather_than_duplicating(self):
        unit = self.unit("1")
        url = reverse("fleet_inspect_vehicle", args=[unit.pk])
        self.client.post(url, {"state_oil": "ok"})
        self.client.post(url, {"state_oil": "flag", "notes": "second look"})
        self.assertEqual(VehicleInspection.objects.filter(vehicle=unit).count(), 1)
        inspection = VehicleInspection.objects.get(vehicle=unit)
        self.assertEqual(inspection.results["oil"]["state"], "flag")
        self.assertEqual(inspection.notes, "second look")

    def test_finding_something_files_an_issue_but_never_takes_the_car_off_the_road(self):
        unit = self.unit("1")
        self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]), {
            "state_brakes": "flag", "found_something": "on",
            "issue_title": "Brakes grinding at low speed", "issue_severity": "ground",
            "notes": "Heard it on the test drive.",
        })
        inspection = VehicleInspection.objects.get(vehicle=unit)
        self.assertTrue(inspection.found_something)
        issue = inspection.issue
        self.assertIsNotNone(issue)
        self.assertEqual(issue.severity, "ground")
        self.assertEqual(issue.source, "fleet")
        self.assertEqual(issue.title, "Brakes grinding at low speed")
        # The ledger is the ONLY thing that removes a unit from the pool.
        self.assertFalse(unit.is_out_of_service_on(TODAY))
        self.assertEqual(VehicleDowntime.objects.count(), 0)

    def test_a_missing_issue_title_is_taken_from_what_was_flagged(self):
        unit = self.unit("1")
        self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]), {
            "state_tires": "flag", "found_something": "on",
        })
        issue = VehicleInspection.objects.get(vehicle=unit).issue
        self.assertIsNotNone(issue)
        self.assertTrue(issue.title)

    def test_a_bad_severity_falls_back_rather_than_500ing(self):
        unit = self.unit("1")
        self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]), {
            "state_oil": "flag", "found_something": "on",
            "issue_title": "Something", "issue_severity": "catastrophic",
        })
        self.assertEqual(VehicleIssue.objects.get().severity, "soon")

    def test_a_bad_odometer_does_not_save_junk(self):
        unit = self.unit("1")
        resp = self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]), {
            "state_oil": "ok", "odometer_miles": "about eighty thousand",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(VehicleInspection.objects.exists())

    def test_logged_out_lands_on_the_real_login_page(self):
        unit = self.unit("1")
        self.client.logout()
        for name, args in (("fleet_inspections", []), ("fleet_inspect_vehicle", [unit.pk])):
            resp = self.client.get(reverse(name, args=args))
            self.assertEqual(resp.status_code, 302, name)
            self.assertTrue(resp.url.startswith(reverse("login")), (name, resp.url))


class StickerBaselineTests(_InspectFixture):
    """The windshield sticker is the only real service record this fleet has.

    It says when the next oil change is DUE; a schedule stores when the last one
    was DONE. Those are the same fact either side of the interval, so reading one
    number off a windscreen produces a baseline nobody had to invent — which is
    what makes the old fleet-wide "stamp today on everything" button unnecessary
    rather than merely removed.
    """

    def _oil(self, unit, interval=5000, **kw):
        from drivers.models import VehicleServiceSchedule
        return VehicleServiceSchedule.objects.create(
            vehicle=unit, service_type="oil", interval_miles=interval, **kw)

    def test_the_sticker_becomes_the_baseline(self):
        unit = self.unit("1")
        schedule = self._oil(unit)
        url = reverse("fleet_inspect_vehicle", args=[unit.pk])
        self.client.post(url, {"state_oil": "ok", "odometer_miles": "157690",
                               "service_due_miles": "162000"})
        schedule.refresh_from_db()
        # due 162,000 minus a 5,000 interval = last done at 157,000
        self.assertEqual(schedule.last_done_odometer_miles, Decimal("157000"))
        self.assertIn("windshield sticker", schedule.notes)

    def test_the_interval_length_is_respected(self):
        unit = self.unit("1", vtype=self.sprinter)
        schedule = self._oil(unit, interval=10000)
        self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]),
                         {"state_oil": "ok", "service_due_miles": "162000"})
        schedule.refresh_from_db()
        self.assertEqual(schedule.last_done_odometer_miles, Decimal("152000"))

    def test_a_real_logged_service_is_never_overwritten(self):
        """A service record is somebody saying what they did. A sticker is an
        estimate written by whoever last held a marker."""
        unit = self.unit("1")
        schedule = self._oil(unit, last_done_odometer_miles=Decimal("150000"),
                             last_done_on=WED)
        self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]),
                         {"state_oil": "ok", "service_due_miles": "162000"})
        schedule.refresh_from_db()
        self.assertEqual(schedule.last_done_odometer_miles, Decimal("150000"))

    def test_a_sticker_below_one_interval_is_refused(self):
        """Almost certainly a typo, and a negative baseline would poison every
        later calculation."""
        unit = self.unit("1")
        schedule = self._oil(unit, interval=5000)
        self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]),
                         {"state_oil": "ok", "service_due_miles": "4000"})
        schedule.refresh_from_db()
        self.assertIsNone(schedule.last_done_odometer_miles)

    def test_no_sticker_changes_nothing(self):
        unit = self.unit("1")
        schedule = self._oil(unit)
        self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]),
                         {"state_oil": "ok", "odometer_miles": "157690"})
        schedule.refresh_from_db()
        self.assertIsNone(schedule.last_done_odometer_miles)

    def test_a_car_with_no_oil_schedule_is_left_alone(self):
        unit = self.unit("1")
        resp = self.client.post(reverse("fleet_inspect_vehicle", args=[unit.pk]),
                                {"state_oil": "ok", "service_due_miles": "162000"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(VehicleInspection.objects.filter(vehicle=unit).exists())
