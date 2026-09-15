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
