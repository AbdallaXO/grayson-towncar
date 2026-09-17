"""What each car has for the day: the per-unit strip, and — above all — the
things the page must refuse to claim.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_day

Leg end times are pinned to pickup + 90 minutes throughout, the same trick
tests_fleet_windows uses, so a gap is arithmetic rather than a drive-time
estimate that changes when the route metrics do.
"""
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse

from dispatching import fleet_day
from dispatching.tests_fleet_desk import DAY, TODAY, _FleetFixture
from drivers.models import Driver, DriverVehicleAssignment, VehicleDowntime


def _ninety_minutes(leg, target_date):
    return datetime.combine(target_date, leg.pickup_time) + timedelta(minutes=90)


class _DayFixture(_FleetFixture):
    """A unit, a chauffeur holding it, and legs on that chauffeur."""

    def hold(self, unit, driver=None, day=DAY):
        driver = driver or self.george
        return DriverVehicleAssignment.objects.create(
            driver=driver, vehicle=unit, date=day)

    def driver(self, username, **kw):
        return Driver.objects.create(
            profile=User.objects.create_user(username, first_name=username.title()),
            driver_type=kw.pop("driver_type", "inhouse"), **kw)

    def job(self, driver, hour, day=DAY, **kw):
        leg = self.leg(day, hour=hour, **kw)
        leg.driver = driver
        leg.save(update_fields=["driver"])
        return leg

    def payload(self, day=DAY):
        return fleet_day.build_day(fleet_day.load_car_day(day))

    def row_for(self, payload, number):
        return next(r for r in payload["rows"] if r["number"] == number)


# ════════════════════════════════════════════════════════════════════════════
# The honesty rules — the reason this page exists
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class HonestyTests(_DayFixture):
    def test_an_unbuilt_day_never_calls_a_car_free(self):
        """No assignment rows at all. Every unit is 'unknown', not 'open' — the
        exact lie the Phase 3 audit found on the desk."""
        self.unit("1")
        self.unit("2")
        payload = self.payload()
        self.assertFalse(payload["built"])
        self.assertEqual({r["state"] for r in payload["rows"]}, {"unknown"})
        self.assertNotIn("free", payload["headline"].lower().replace("free car", ""))
        for row in payload["rows"]:
            self.assertNotIn("free", row["note"].lower())

    def test_an_unbuilt_day_still_reports_what_is_booked(self):
        """Going blank would read as 'nothing on'. The booked count is the fact
        that makes the empty page honest."""
        self.unit("1")
        self.leg(DAY, hour=9)
        self.leg(DAY, hour=11)
        payload = self.payload()
        self.assertEqual(payload["total_legs"], 2)
        self.assertIn("2 trips are booked", payload["headline"])

    def test_a_half_built_day_softens_every_empty_car(self):
        """Coverage below the confidence line means an empty strip is 'not
        assigned yet', never 'free'."""
        unit_a, unit_b = self.unit("1"), self.unit("2")
        held = self.driver("busy_one")
        self.hold(unit_a, held)
        self.hold(unit_b, self.george)
        self.job(held, 9)
        # Four legs with nobody on them drags coverage to 20%.
        for hour in (10, 12, 14, 16):
            self.leg(DAY, hour=hour)
        payload = self.payload()
        self.assertTrue(payload["built"])
        self.assertFalse(payload["confident"])
        self.assertLess(payload["coverage_pct"], 90)
        empty = self.row_for(payload, "2")
        self.assertEqual(empty["state"], "unknown")
        self.assertNotIn("free", empty["note"].lower())

    def test_a_confident_day_does_say_a_car_is_free(self):
        """The flip side: when the board really is built, the page must commit."""
        unit_a, unit_b = self.unit("1"), self.unit("2")
        working = self.driver("working_one")
        self.hold(unit_a, working)
        self.job(working, 9)
        payload = self.payload()
        self.assertTrue(payload["confident"])
        self.assertEqual(payload["coverage_pct"], 100)
        self.assertEqual(self.row_for(payload, "2")["state"], "open")
        self.assertIn("No chauffeur", self.row_for(payload, "2")["note"])

    def test_a_car_in_the_shop_reads_as_down_not_free(self):
        unit = self.unit("1")
        VehicleDowntime.objects.create(
            vehicle=unit, category="maintenance", reason="Oil change",
            starts_on=DAY - timedelta(days=1), expected_back_on=DAY + timedelta(days=1))
        other = self.unit("2")
        held = self.driver("someone")
        self.hold(other, held)
        self.job(held, 9)
        row = self.row_for(self.payload(), "1")
        self.assertEqual(row["state"], "down")
        self.assertNotIn("free", row["note"].lower())


# ════════════════════════════════════════════════════════════════════════════
# The strip itself
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class StripTests(_DayFixture):
    def test_a_cars_jobs_come_from_the_chauffeur_holding_it(self):
        unit = self.unit("1")
        held = self.driver("holder")
        self.hold(unit, held)
        self.job(held, 8)
        self.job(held, 13)
        row = self.row_for(self.payload(), "1")
        self.assertEqual(row["trips"], 2)
        self.assertEqual(row["state"], "working")
        self.assertEqual([j["start_label"] for j in row["jobs"]], ["8:00 AM", "1:00 PM"])

    def test_a_legs_of_a_driver_on_another_car_do_not_leak_in(self):
        mine, theirs = self.unit("1"), self.unit("2")
        me, them = self.driver("me"), self.driver("them")
        self.hold(mine, me)
        self.hold(theirs, them)
        self.job(me, 8)
        self.job(them, 9)
        self.assertEqual(self.row_for(self.payload(), "1")["trips"], 1)

    def test_rows_come_back_in_unit_number_order(self):
        """#2 before #10 before #14, whatever each car is doing. A car that
        changes rows between mornings is a car you have to hunt for."""
        for number in ("14", "2", "10", "1"):
            self.unit(number)
        payload = self.payload()
        self.assertEqual([r["number"] for r in payload["rows"]],
                         ["1", "2", "10", "14"])

    def test_a_car_in_the_shop_keeps_its_place_in_the_numbering(self):
        """It is marked down on the row, not banished to the bottom."""
        self.unit("1")
        shopped = self.unit("2")
        self.unit("3")
        VehicleDowntime.objects.create(
            vehicle=shopped, category="repair", reason="Gearbox",
            starts_on=DAY, expected_back_on=DAY + timedelta(days=3))
        payload = self.payload()
        self.assertEqual([r["number"] for r in payload["rows"]], ["1", "2", "3"])
        self.assertEqual(self.row_for(payload, "2")["state"], "down")

    def test_a_shared_car_merges_both_chauffeurs_in_time_order(self):
        """One physical unit, an AM and a PM driver — the audit's shared-car case.
        The desk's own assigned_today dict overwrites here; this must not."""
        unit = self.unit("1")
        am, pm = self.driver("am_driver"), self.driver("pm_driver")
        self.hold(unit, am)
        self.hold(unit, pm)
        self.job(am, 6)
        self.job(pm, 15)
        row = self.row_for(self.payload(), "1")
        self.assertTrue(row["shared"])
        self.assertEqual(len(row["drivers"]), 2)
        self.assertEqual(row["trips"], 2)
        self.assertEqual([j["start_label"] for j in row["jobs"]], ["6:00 AM", "3:00 PM"])

    def test_the_seam_between_two_chauffeurs_is_a_handoff_not_a_window(self):
        """A long hole across a driver change is the car changing hands, and must
        never be offered as shop time."""
        unit = self.unit("1")
        am, pm = self.driver("am2"), self.driver("pm2")
        self.hold(unit, am)
        self.hold(unit, pm)
        self.job(am, 6)
        self.job(pm, 15)
        row = self.row_for(self.payload(), "1")
        self.assertEqual(len(row["gaps"]), 1)
        self.assertTrue(row["gaps"][0]["handoff"])
        self.assertFalse(row["gaps"][0]["usable"])
        self.assertEqual(row["usable_windows"], [])

    def test_a_long_hole_on_one_shift_is_offered_as_shop_time(self):
        unit = self.unit("1")
        held = self.driver("gapper")
        self.hold(unit, held)
        self.job(held, 7)          # clears 8:30
        self.job(held, 14)         # 5h 30m hole
        row = self.row_for(self.payload(), "1")
        self.assertEqual(len(row["usable_windows"]), 1)
        self.assertEqual(row["longest_gap"]["span"], "5h 30m")

    def test_a_short_hole_is_not_offered(self):
        unit = self.unit("1")
        held = self.driver("tight")
        self.hold(unit, held)
        self.job(held, 9)          # clears 10:30
        self.job(held, 11)         # 30m hole
        row = self.row_for(self.payload(), "1")
        self.assertEqual(row["usable_windows"], [])
        self.assertEqual(row["longest_gap"]["span"], "30m")

    def test_a_window_after_an_airport_arrival_needs_to_be_longer(self):
        """The arrival's pickup time moves with the flight, so the clock
        overstates the hole behind it. Same 2h gap: usable after a normal drop,
        not usable after an arrival."""
        plain, after_arrival = self.unit("1"), self.unit("2")
        a, b = self.driver("plain_d"), self.driver("arr_d")
        self.hold(plain, a)
        self.hold(after_arrival, b)
        # 2h 30m hole in both cases (job clears at +90m, next at +4h).
        self.job(a, 9, pickup="Disney", dropoff="Disney")
        self.job(a, 13, pickup="Disney", dropoff="Disney")
        self.job(b, 9, pickup="MCO Terminal B", dropoff="Disney")
        self.job(b, 13, pickup="Disney", dropoff="Disney")
        rows = self.payload()
        plain_gap = self.row_for(rows, "1")["gaps"][0]
        arrival_gap = self.row_for(rows, "2")["gaps"][0]
        self.assertEqual(plain_gap["minutes"], arrival_gap["minutes"])
        self.assertTrue(arrival_gap["flight_dependent"])
        self.assertFalse(plain_gap["flight_dependent"])
        self.assertTrue(plain_gap["usable"])
        self.assertFalse(arrival_gap["usable"])

    def test_a_job_running_past_midnight_does_not_wrap_to_the_start(self):
        """`.hour` arithmetic would put a 23:44 pickup's clear time at 01:14 and
        draw it at the far LEFT of the page. The axis is datetimes."""
        unit = self.unit("1")
        held = self.driver("latenight")
        self.hold(unit, held)
        self.job(held, 23)
        payload = self.payload()
        row = self.row_for(payload, "1")
        self.assertGreater(payload["axis_end"], datetime.combine(DAY, time(23, 0)))
        self.assertGreater(row["jobs"][0]["left"], 50)

    def test_a_departed_chauffeur_does_not_appear_to_hold_a_car(self):
        unit = self.unit("1")
        gone = self.driver("departed")
        gone.is_active = False
        gone.save(update_fields=["is_active"])
        self.hold(unit, gone)
        self.job(gone, 9)
        row = self.row_for(self.payload(), "1")
        self.assertEqual(row["drivers"], [])
        self.assertEqual(row["trips"], 0)

    def test_trips_on_no_in_house_car_are_counted_not_dropped(self):
        """A farmed or unassigned leg belongs to no unit. Silently losing it would
        under-report the day."""
        unit = self.unit("1")
        held = self.driver("only_one")
        self.hold(unit, held)
        self.job(held, 9)
        self.leg(DAY, hour=10)          # nobody on it
        payload = self.payload()
        self.assertEqual(payload["total_legs"], 2)
        self.assertEqual(payload["unplaced"], 1)


# ════════════════════════════════════════════════════════════════════════════
# The page
# ════════════════════════════════════════════════════════════════════════════

@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class PageTests(_DayFixture):
    def test_it_renders_and_defaults_to_today(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_day"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["day"], TODAY)
        self.assertEqual(resp.context["fleet_page"], "day")

    def test_a_date_can_be_asked_for(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_day"), {"date": DAY.isoformat()})
        self.assertEqual(resp.context["day"], DAY)

    def test_a_nonsense_date_falls_back_to_today_rather_than_500ing(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_day"), {"date": "yesterday-ish"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["day"], TODAY)

    def test_logged_out_lands_on_the_real_login_page(self):
        """Same rule as every other fleet URL: /accounts/login/ is a 404 here."""
        self.client.logout()
        resp = self.client.get(reverse("fleet_day"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith(reverse("login")))

    def test_the_page_offers_only_days_the_board_is_built_for(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_day"))
        options = resp.context["day_options"]
        self.assertEqual(len(options), fleet_day.DAY_CHOICES)
        self.assertEqual(options[0]["date"], TODAY)
        self.assertEqual(options[0]["label"], "Today")


class HelperTests(_FleetFixture):
    def test_span_reads_as_words(self):
        self.assertEqual(fleet_day._span(45), "45m")
        self.assertEqual(fleet_day._span(60), "1h")
        self.assertEqual(fleet_day._span(190), "3h 10m")

    def test_coverage_of_an_empty_day_is_not_zero_percent(self):
        """No legs means nothing to be wrong about — 0/0 must not read as 0%,
        which would flip the page into its 'still being built' warning."""
        self.assertEqual(fleet_day.coverage([]), (0, 0, 1.0))

    def test_parse_day_survives_junk(self):
        self.assertEqual(fleet_day.parse_day(None, TODAY), TODAY)
        self.assertEqual(fleet_day.parse_day("", TODAY), TODAY)
        self.assertEqual(fleet_day.parse_day("2026-02-30", TODAY), TODAY)
        self.assertEqual(fleet_day.parse_day("2026-03-01", TODAY).isoformat(), "2026-03-01")


class DeskAgreesWithTheDayTests(_DayFixture):
    """The desk and The day must not contradict each other about a free car.

    Audit bug 2: every fleet surface read "no assignment row" as "free", which
    before Day Setup runs is true of the whole fleet.
    """

    def test_the_desk_does_not_call_the_fleet_free_before_the_board_is_built(self):
        from dispatching import fleet_desk
        self.unit("1")
        self.unit("2")
        desk = fleet_desk.load_desk()
        self.assertFalse(desk["built"])
        self.assertEqual(desk["idle_today"], [])
        self.assertEqual(desk["summary"]["idle_today"], 0)
        self.assertIn("not built", desk["shop"]["idle_text"].lower())
        # ...and it must not flip to the opposite claim either.
        self.assertNotIn("every unit has a chauffeur", desk["shop"]["idle_text"].lower())

    def test_the_desk_still_names_free_cars_once_the_board_is_built(self):
        from dispatching import fleet_desk
        working, spare = self.unit("1"), self.unit("2")
        driver = self.driver("desk_holder")
        DriverVehicleAssignment.objects.create(
            driver=driver, vehicle=working, date=TODAY)
        desk = fleet_desk.load_desk()
        self.assertTrue(desk["built"])
        self.assertEqual([r["vehicle"].id for r in desk["idle_today"]], [spare.id])
        self.assertIn("no chauffeur today", desk["shop"]["idle_text"].lower())


# ════════════════════════════════════════════════════════════════════════════
# The now-line
#
# Drawn straight from the payload, so the only thing worth protecting is that
# it is never drawn on a day it would be lying about.
# ════════════════════════════════════════════════════════════════════════════

class NowLineTests(_DayFixture):
    def build(self, now):
        loaded = fleet_day.load_car_day(DAY)
        loaded["now"] = now
        return fleet_day.build_day(loaded)

    def test_it_sits_where_the_clock_says(self):
        unit = self.unit("1")
        self.job(self.george, 8, day=DAY)
        self.hold(unit)
        payload = self.build(None)
        start, end = payload["axis_start"], payload["axis_end"]
        middle = start + (end - start) / 2
        self.assertAlmostEqual(self.build(middle)["now_pct"], 50.0, places=1)
        self.assertEqual(self.build(start)["now_pct"], 0.0)

    def test_there_is_no_line_on_a_day_that_is_not_today(self):
        """Half past two on tomorrow's board is not half past two on tomorrow."""
        unit = self.unit("1")
        self.job(self.george, 8, day=DAY)
        self.hold(unit)
        payload = self.build(datetime.combine(DAY - timedelta(days=1), time(14, 30)))
        self.assertIsNone(payload["now_pct"])
        self.assertEqual(payload["now_label"], "")

    def test_an_hour_before_the_day_starts_draws_nothing(self):
        """At 2:30 AM the board's axis has not opened yet. A line clamped to the
        left edge would read as 'the day has begun', which it has not."""
        unit = self.unit("1")
        self.job(self.george, 8, day=DAY)
        self.hold(unit)
        payload = self.build(datetime.combine(DAY, time(2, 30)))
        self.assertIsNone(payload["now_pct"])


# ════════════════════════════════════════════════════════════════════════════
# Which holes the strip marks in gold
#
# The complaint these protect, in the founder's words: "for miguelk it shows he
# has a gap between 1:25 PM to 3:10 PM, that is about 2 hours, it doesn't
# highlight that, but for yovanny it highlights 3h+ free even tho that is at
# 8+ PM." Both were the same fault — the strip was marking holes long enough for
# a SHOP VISIT (90 minutes of service plus 30 to get the car to the bay and
# back) on the screen belonging to the man who walks the cars himself and goes
# home at four.
# ════════════════════════════════════════════════════════════════════════════

SHIFT = (time(7, 30), time(16, 0))


class ReachableWindowTests(_DayFixture):
    def marks(self, row):
        return [(g["reachable"]["from_label"], g["reachable"]["to_label"],
                 g["reachable"]["span"]) for g in row["gaps"] if g["reachable"]]

    def gap(self, start, end, **kw):
        a = datetime.combine(DAY, start)
        b = datetime.combine(DAY, end)
        base = {"start": a, "end": b,
                "minutes": int((b - a).total_seconds() // 60),
                "handoff": False, "flight_dependent": False}
        base.update(kw)
        return base

    def test_an_afternoon_gap_of_an_hour_and_three_quarters_is_marked(self):
        """Miguel's. It is short of the shop's two hours and always was."""
        window = fleet_day.reachable_window(
            self.gap(time(13, 25), time(15, 10)), DAY, SHIFT)
        self.assertIsNotNone(window)
        self.assertEqual(window["span"], "1h 45m")
        self.assertEqual(window["from_label"], "1:25 PM")

    def test_a_three_hour_gap_in_the_evening_is_not_marked(self):
        """Yovanny's. Long enough for the shop, useless to the walker."""
        self.assertIsNone(fleet_day.reachable_window(
            self.gap(time(20, 7), time(23, 12)), DAY, SHIFT))

    def test_the_small_hours_are_not_marked_either(self):
        self.assertIsNone(fleet_day.reachable_window(
            self.gap(time(3, 30), time(6, 15)), DAY, SHIFT))

    def test_a_gap_running_past_the_end_of_shift_is_marked_only_to_the_end(self):
        """Marking it to six o'clock would send him out to a car that left at
        four."""
        window = fleet_day.reachable_window(
            self.gap(time(14, 0), time(18, 0)), DAY, SHIFT)
        self.assertEqual(window["to_label"], "4:00 PM")
        self.assertEqual(window["span"], "2h")

    def test_the_founders_own_worked_example_clears(self):
        """#008 clears MCO at 8:30, is at HQ by 8:45, walked by 9:20, and makes
        a 9:40 arrival. Seventy minutes, and he calls it comfortable."""
        self.assertIsNotNone(fleet_day.reachable_window(
            self.gap(time(8, 30), time(9, 40)), DAY, SHIFT))

    def test_three_quarters_of_an_hour_does_not(self):
        """Twenty minutes of walking round the car, and half an hour of getting
        it to HQ and back out. Forty-five minutes is not fifty."""
        self.assertIsNone(fleet_day.reachable_window(
            self.gap(time(10, 0), time(10, 45)), DAY, SHIFT))

    def test_a_gap_behind_an_airport_arrival_still_pays_the_flight_hour(self):
        """It starts when the aircraft lands, so an hour of it is not there —
        and seventy minutes that would otherwise clear does not."""
        self.assertIsNone(fleet_day.reachable_window(
            self.gap(time(8, 30), time(9, 40), flight_dependent=True), DAY, SHIFT))
        self.assertIsNotNone(fleet_day.reachable_window(
            self.gap(time(8, 30), time(10, 45), flight_dependent=True), DAY, SHIFT))

    def test_a_handoff_is_never_marked(self):
        self.assertIsNone(fleet_day.reachable_window(
            self.gap(time(10, 0), time(13, 0), handoff=True), DAY, SHIFT))

    def test_an_evening_shift_marks_the_evening_gap(self):
        """The hours belong to the reader, which is the whole reason the strip
        takes them rather than a company constant."""
        window = fleet_day.reachable_window(
            self.gap(time(20, 7), time(23, 12)), DAY, (time(15, 0), time(23, 59)))
        self.assertEqual(window["from_label"], "8:07 PM")

    def test_the_strip_and_the_inspection_round_agree_about_one_car(self):
        """They must: two screens disagreeing about whether #7 is free this
        afternoon is worse than neither of them saying anything."""
        from dispatching import fleet_inspection
        unit = self.unit("7")
        self.hold(unit)
        self.job(self.george, 8, day=DAY)
        self.job(self.george, 15, day=DAY)
        row = self.row_for(
            fleet_day.build_day(fleet_day.load_car_day(DAY), shift=SHIFT), "7")
        strip = self.marks(row)
        round_ = fleet_inspection.walkable_window(row, DAY, SHIFT)
        if strip:
            best = max(row["reachable_windows"],
                       key=lambda g: g["reachable"]["minutes"])["reachable"]
            self.assertEqual((round_["from_label"], round_["to_label"]),
                             (best["from_label"], best["to_label"]))
        else:
            self.assertIsNone(round_)
