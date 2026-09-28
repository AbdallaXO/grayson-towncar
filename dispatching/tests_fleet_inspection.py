"""The weekly inspection round: the reset, the suggestion, and the form.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_inspection

The two rules worth protecting are that the week resets itself on Monday with
nothing to run, and that walking the same car twice never makes two records the
count would double.
"""
from datetime import date, datetime, time, timedelta
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


# The fleet manager's working day. Windows outside it are not windows he can use.
SHIFT = (time(7, 30), time(16, 0))


def _window(day, start, end, *, handoff=False, flight=False):
    """One hole in a car's day, shaped the way ``fleet_day.gaps_between`` builds
    them. ``needed`` is deliberately the SHOP figure here, as it is in real rows
    — the round must not be reading it."""
    from business.datefmt import time12
    a, b = datetime.combine(day, start), datetime.combine(day, end)
    return {"start": a, "end": b, "needed": 120,
            "minutes": int((b - a).total_seconds() // 60),
            "handoff": handoff, "flight_dependent": flight,
            "usable": True, "from_label": time12(a), "to_label": time12(b)}


def _working(number, windows, trips=6):
    windows = list(windows)
    return {"number": number, "state": "working", "trips": trips, "note": "",
            "longest_gap": windows[0] if windows else None,
            "gaps": windows,
            "usable_windows": [w for w in windows if w["minutes"] >= 120]}


class _InspectFixture(_FleetFixture):
    def week(self, day=WED, day_rows=None, shift=None):
        loaded = fleet_inspection.load_week(day)
        return fleet_inspection.build_week(
            loaded, day_rows=day_rows, shift=shift,
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


# ════════════════════════════════════════════════════════════════════════════
# The round is walked by a person on a shift
#
# The complaint these protect: the round offered cars "Free 7:04 PM – 11:04 PM"
# to a manager who goes home at four.
# ════════════════════════════════════════════════════════════════════════════

class ShiftTests(_InspectFixture):
    def test_an_evening_window_is_not_a_window(self):
        """The exact tile the fleet manager complained about."""
        self.unit("1")
        rows = [_working("1", [_window(WED, time(19, 4), time(23, 4))])]
        tile = self.tile(self.week(day_rows=rows, shift=SHIFT), "1")
        self.assertTrue(tile["off_shift"])
        self.assertEqual(tile["window_minutes"], 0)
        self.assertNotIn("7:04 PM", tile["today"])

    def test_it_says_why_instead_of_naming_a_time_it_cannot_use(self):
        self.unit("1")
        rows = [_working("1", [_window(WED, time(19, 4), time(23, 4))])]
        tile = self.tile(self.week(day_rows=rows, shift=SHIFT), "1")
        self.assertEqual(tile["today"], "No gap before 4:00 PM")

    def test_a_morning_window_survives_and_is_named(self):
        self.unit("1")
        rows = [_working("1", [_window(WED, time(9, 0), time(11, 30))])]
        tile = self.tile(self.week(day_rows=rows, shift=SHIFT), "1")
        self.assertFalse(tile["off_shift"])
        self.assertEqual(tile["today"], "Free 9:00 AM – 11:30 AM")

    def test_an_hour_in_the_middle_of_the_day_is_enough_to_walk_a_car(self):
        """The round used to measure a walk-around against the cost of a garage
        visit — 90 minutes of service plus 30 to get the car to the bay and back
        — and so called a clear hour unreachable. Nothing is driven anywhere."""
        self.unit("1")
        rows = [_working("1", [_window(WED, time(10, 0), time(11, 8))])]
        tile = self.tile(self.week(day_rows=rows, shift=SHIFT), "1")
        self.assertFalse(tile["off_shift"])
        self.assertEqual(tile["today"], "Free 10:00 AM – 11:08 AM")

    def test_a_gap_behind_an_airport_arrival_still_pays_the_flight_hour(self):
        """It starts when the aircraft lands, so an hour of it is not there."""
        self.unit("1")
        self.unit("2")
        rows = [_working("1", [_window(WED, time(10, 0), time(11, 8), flight=True)]),
                _working("2", [_window(WED, time(10, 0), time(11, 8))])]
        week = self.week(day_rows=rows, shift=SHIFT)
        self.assertTrue(self.tile(week, "1")["off_shift"])
        self.assertFalse(self.tile(week, "2")["off_shift"])

    def test_a_handoff_is_not_a_window(self):
        """The one gap where the car may be moving rather than parked."""
        self.unit("1")
        rows = [_working("1", [_window(WED, time(10, 0), time(12, 0), handoff=True)])]
        self.assertTrue(self.tile(self.week(day_rows=rows, shift=SHIFT), "1")["off_shift"])

    def test_a_window_straddling_the_end_of_shift_is_cut_at_the_end_of_shift(self):
        """Free 2 PM – 6 PM is two hours to a man who leaves at four, and the
        tile must say four, not six — he would stand there waiting for a car
        that is already gone."""
        self.unit("1")
        rows = [_working("1", [_window(WED, time(14, 0), time(18, 0))])]
        tile = self.tile(self.week(day_rows=rows, shift=SHIFT), "1")
        self.assertEqual(tile["today"], "Free 2:00 PM – 4:00 PM")
        self.assertEqual(tile["window_minutes"], 120)

    def test_a_straddling_window_too_short_once_cut_is_dropped(self):
        """Fifteen minutes of a three-hour gap falling before four o'clock is
        fifteen minutes, and you cannot walk a car in fifteen minutes."""
        self.unit("1")
        rows = [_working("1", [_window(WED, time(15, 45), time(19, 0))])]
        tile = self.tile(self.week(day_rows=rows, shift=SHIFT), "1")
        self.assertTrue(tile["off_shift"])

    def test_a_reachable_car_is_suggested_over_an_evening_one(self):
        self.unit("1")
        self.unit("2")
        rows = [_working("1", [_window(WED, time(19, 0), time(23, 0))]),
                _working("2", [_window(WED, time(9, 0), time(11, 30))])]
        week = self.week(day_rows=rows, shift=SHIFT)
        self.assertEqual([t["number"] for t in week["suggested"]], ["2"])
        self.assertFalse(week["suggestion_is_fallback"])

    def test_the_list_is_not_padded_with_cars_he_cannot_reach(self):
        """Five tiles where four are out all day is a worse morning than one
        tile that is true."""
        for n in range(1, 7):
            self.unit(str(n))
        evening = [_window(WED, time(19, 0), time(23, 0))]
        rows = [_working("2", evening), _working("3", evening), _working("4", evening),
                _working("5", evening), _working("6", evening),
                _working("1", [_window(WED, time(9, 0), time(11, 30))])]
        week = self.week(day_rows=rows, shift=SHIFT)
        self.assertEqual([t["number"] for t in week["suggested"]], ["1"])

    def test_with_nothing_reachable_it_offers_one_car_and_admits_it(self):
        """The round never goes silent — but it never pretends either."""
        for n in range(1, 4):
            self.unit(str(n))
        evening = [_window(WED, time(19, 0), time(23, 0))]
        week = self.week(day_rows=[_working(str(n), evening) for n in range(1, 4)],
                         shift=SHIFT)
        self.assertEqual(len(week["suggested"]), 1)
        self.assertTrue(week["suggestion_is_fallback"])
        self.assertEqual(week["off_shift_count"], 3)

    def test_an_unbuilt_board_is_not_an_unreachable_fleet(self):
        """Silence about a car is not a claim that it is out. Day Setup not
        having run must never empty the round."""
        for n in range(1, 4):
            self.unit(str(n))
        week = self.week(day_rows=None, shift=SHIFT)
        self.assertEqual(len(week["suggested"]), 3)
        self.assertFalse(week["suggestion_is_fallback"])

    def test_a_car_standing_still_is_free_for_the_whole_shift(self):
        self.unit("1")
        rows = [{"number": "1", "state": "open", "trips": 0,
                 "note": "No chauffeur", "longest_gap": None, "usable_windows": []}]
        tile = self.tile(self.week(day_rows=rows, shift=SHIFT), "1")
        self.assertEqual(tile["window_minutes"], 510)      # 7:30 to 4:00
        self.assertEqual(tile["today"], "Sitting still today")

    def test_an_evening_shift_gets_the_evening_windows(self):
        """The hours are the person's, not the company's — which is the whole
        reason they live on a profile."""
        self.unit("1")
        rows = [_working("1", [_window(WED, time(19, 4), time(23, 4))])]
        tile = self.tile(
            self.week(day_rows=rows, shift=(time(15, 0), time(23, 59))), "1")
        self.assertFalse(tile["off_shift"])
        self.assertEqual(tile["today"], "Free 7:04 PM – 11:04 PM")
