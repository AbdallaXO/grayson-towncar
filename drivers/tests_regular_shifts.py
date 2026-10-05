"""The regular-shift module (structured shifts, Stage 1): suggestions from the
last 8 weeks, validation, saving, labels, the roster and the switch — plus the
usual shift per driver, Float, and the per-day options (a second shape, a
day's own limits, blank times meaning the shape's usual times) from Task 3b.

Two review-focus guards live here: confirming a regular shift must not move
anything the legacy availability resolver returns (Review Focus 2), and saving
either legacy schedule editor must leave a confirmed regular shift alone
(Review Focus 1).

And the pages (Task 7): Drivers -> Regular Shifts (who still needs a regular
shift and who has one), the editor a manager confirms a week from, the switch
that hands regular shifts to auto-assign, the navbar link and the profile
card's link. Review Focus 3 (a driver added after the switch is on) and 4 (no
recent trips) are checked on the pages too.

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_regular_shifts
"""
import copy
import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dispatching import day_setup
from dispatching.models import REGULAR_WINDOWS_CACHE_KEY, SchedulerSettings
from drivers import regular_shifts as rs
from drivers.availability import resolve_effective_availability
from drivers.models import Driver, DriverWeeklySchedule, ShiftTemplate
from drivers.regular_shifts import DayShift
from drivers.test_support import RegularShiftCacheMixin
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

TODAY = date(2026, 10, 5)                       # a Monday
MCO = "MCO Terminal B"                          # categorize_location -> "MCO Terminal"
DISNEY = "Disney's Contemporary Resort"         # categorize_location -> "Disney Resort"
PORT = "Port Canaveral Cruise Terminal 3"       # categorize_location -> "Port Canaveral Area"
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_NO_LIMITS = dict(hard_earliest_start=None, hard_latest_finish=None,
                  hard_latest_finish_next_day=False, max_days_per_week=None)


def _lookback(weekday):
    """The 8 dates with this weekday in [TODAY - 56d, TODAY), newest first."""
    start = TODAY - timedelta(days=rs.LOOKBACK_DAYS)
    days = [start + timedelta(days=i) for i in range(rs.LOOKBACK_DAYS)]
    return sorted((d for d in days if d.weekday() == weekday), reverse=True)


_driver_seq = 0


def _driver(first="Sam", last="Driver", **kw):
    global _driver_seq
    _driver_seq += 1
    user = User.objects.create_user(username=f"rs_{first.lower()}_{_driver_seq}",
                                    first_name=first, last_name=last)
    kw.setdefault("driver_type", "inhouse")
    return Driver.objects.create(profile=user, **kw)


class _Fixture(RegularShiftCacheMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.route = Route.objects.create(origin=Location.objects.create(name="MCO"),
                                         destination=Location.objects.create(name="Disney"),
                                         inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(vehicle=cls.suv, route=cls.route,
                                       oneway_price=Decimal("100.00"),
                                       round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(first_name="John", last_name="Doe",
                                               email="j@example.com", phone_number="5551234567")
        cls.manager = User.objects.create_user("rs_manager", password="x",
                                               is_staff=True, is_superuser=True)
        cls.dispatcher = User.objects.create_user("rs_dispatcher", password="x", is_staff=True)

    def setUp(self):
        super().setUp()
        self.t = {t.kind: t for t in ShiftTemplate.objects.all()}

    def leg(self, driver, day, hh, mm=0, pickup=MCO, dropoff=DISNEY,
            status="confirmed", res_status="confirmed"):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate, vehicle=self.suv,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"), status=res_status)
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=time(hh, mm), driver=driver,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route, status=status)

    def week(self, **days):
        """Seven DayShifts; a day given as (kind, start, end) works, the rest are Off.
        An optional fourth item holds the day's options: alt (a kind), day_earliest,
        day_latest and day_latest_next_day."""
        out = []
        for i, key in enumerate(_DAY_KEYS):
            spec = days.get(key)
            if spec is None:
                out.append(DayShift(i, None, None, None))
                continue
            kind, start, end, *rest = spec
            extra = dict(rest[0]) if rest else {}
            if "alt" in extra:
                extra["alt_template_id"] = self.t[extra.pop("alt")].id
            out.append(DayShift(i, self.t[kind].id, start, end, **extra))
        return out

    def validate(self, days, templates=None, rest_min=510, **limits):
        return rs.validate_regular_shift(days, templates=templates or rs.templates_by_id(),
                                         rest_min=rest_min, **{**_NO_LIMITS, **limits})


# ════════════════════════════════════════════════════════════════════════════
# Suggestions from the last 8 weeks (S6)
# ════════════════════════════════════════════════════════════════════════════

class SuggestTests(_Fixture):
    def test_suggest_regular_weekday(self):
        d = _driver()
        for day in _lookback(0)[:5]:
            self.leg(d, day, 5, 0, MCO, DISNEY)
            self.leg(d, day, 9, 0, DISNEY, MCO)
        monday = rs.suggest_regular_shifts([d], TODAY)[d.id][0]
        self.assertEqual(monday.day, 0)
        self.assertEqual(monday.weeks_worked, 5)
        self.assertEqual(monday.template_id, self.t["morning"].id)
        # 05:00 - (12 drive + 10 airport buffer) = 04:38, rounded down to 04:35.
        self.assertEqual(monday.start, time(4, 35))
        # Last clear 09:00 + 34.8 (P50 departure tail) + 12 (MCO -> base)
        # = 09:46.8, rounded up to 09:50.
        self.assertEqual(monday.end, time(9, 50))

    def test_suggest_irregular_weekday_is_off(self):
        d = _driver()
        for day in _lookback(1)[:3]:
            self.leg(d, day, 5, 0)
        tuesday = rs.suggest_regular_shifts([d], TODAY)[d.id][1]
        self.assertEqual(tuesday.weeks_worked, 3)
        self.assertIsNone(tuesday.template_id)
        self.assertIsNone(tuesday.start)
        self.assertIsNone(tuesday.end)

    def test_suggest_evening_start_and_end_are_drive_only(self):
        # Stage 1 counts only the drive (07 §13 K10): an Evening start has no report
        # offset and its end no end-of-night wash.
        d = _driver()
        firsts = [(14, 50), (15, 0), (15, 0), (15, 10), (15, 20)]
        for day, (hh, mm) in zip(_lookback(2), firsts):
            self.leg(d, day, hh, mm, MCO, DISNEY)
            self.leg(d, day, 23, 0, MCO, MCO)
        wednesday = rs.suggest_regular_shifts([d], TODAY)[d.id][2]
        self.assertEqual(wednesday.template_id, self.t["evening"].id)
        # Raw starts 14:28..14:58 (pickup - 22) sit in the Evening band -> median
        # 14:38, rounded down to 14:35.
        self.assertEqual(wednesday.start, time(14, 35))
        # 23:00 + 75.5 (P50 arrival tail) + 12 (MCO -> base) = 00:27.5 -> 00:30.
        self.assertEqual(wednesday.end, time(0, 30))

    def test_suggest_night_tail_counts_for_previous_day(self):
        d = _driver()
        for day in _lookback(3)[:5]:                       # Thursdays
            self.leg(d, day, 16, 0, MCO, DISNEY)
            self.leg(d, day + timedelta(days=1), 0, 30, MCO, MCO)   # Friday 00:30
        days = rs.suggest_regular_shifts([d], TODAY)[d.id]
        thursday, friday = days[3], days[4]
        self.assertEqual(thursday.template_id, self.t["evening"].id)
        self.assertEqual(thursday.start, time(15, 35))     # 16:00 - 22 = 15:38 -> 15:35
        # 00:30 next day + 75.5 + 12 = 01:57.5 -> 02:00, inside 15:35 + 12h.
        self.assertEqual(thursday.end, time(2, 0))
        self.assertEqual(friday.weeks_worked, 0)
        self.assertIsNone(friday.template_id)

    def test_suggest_end_uses_each_legs_own_return(self):
        # The leg that clears last isn't always the last one back at base: the
        # Port leg clears ~22 min earlier but has a far longer drive home.
        d = _driver()
        for day in _lookback(2)[:5]:                       # Wednesdays
            self.leg(d, day, 15, 0, MCO, DISNEY)
            self.leg(d, day, 20, 0, MCO, MCO)              # clears 21:15.5, back 21:27.5
            self.leg(d, day, 20, 0, PORT, PORT)            # clears 20:53.6, back 21:43.6
        wednesday = rs.suggest_regular_shifts([d], TODAY)[d.id][2]
        self.assertEqual(wednesday.template_id, self.t["evening"].id)
        self.assertEqual(wednesday.start, time(14, 35))
        # 20:00 + 53.6 (P50 other tail) + 50 (Port -> base) = 21:43.6 -> 21:45;
        # the MCO leg alone would give 21:30.
        self.assertEqual(wednesday.end, time(21, 45))

    def test_suggest_start_uses_each_legs_own_lead(self):
        # The first pickup isn't always the first to leave base: the 05:30
        # Port pickup needs 65 min from base, the 05:00 MCO pickup only 22.
        d = _driver()
        for day in _lookback(0)[:5]:
            self.leg(d, day, 5, 0, MCO, DISNEY)
            self.leg(d, day, 5, 30, PORT, DISNEY)
        monday = rs.suggest_regular_shifts([d], TODAY)[d.id][0]
        self.assertEqual(monday.template_id, self.t["morning"].id)
        self.assertEqual(monday.start, time(4, 25))        # 05:30 - 65, not 05:00 - 22
        # 05:30 + 53.6 (P50 other tail) + 35 (Disney -> base) = 06:58.6 -> 07:00.
        self.assertEqual(monday.end, time(7, 0))

    def test_suggest_night_only_day_stays_on_its_own_date(self):
        # Every Friday's only work is a 01:30 pickup and Thursday wasn't worked:
        # that is early Friday work, not a Thursday Evening ~24h too soon.
        d = _driver()
        for day in _lookback(3)[:5]:                       # Thursdays, left idle
            self.leg(d, day + timedelta(days=1), 1, 30, MCO, MCO)
        days = rs.suggest_regular_shifts([d], TODAY)[d.id]
        thursday, friday = days[3], days[4]
        self.assertEqual((thursday.weeks_worked, thursday.template_id), (0, None))
        self.assertEqual(friday.weeks_worked, 5)
        self.assertEqual(friday.template_id, self.t["morning"].id)
        # 01:30 - 22 = 01:08 -> 01:05; 01:30 + 75.5 + 12 = 02:57.5 -> 03:00.
        self.assertEqual((friday.start, friday.end), (time(1, 5), time(3, 0)))
        self.assertEqual(DayShift(4, friday.template_id, friday.start, friday.end).minutes(),
                         (65, 180))

    def test_suggest_start_never_before_midnight(self):
        # A 00:10 pickup with nothing the evening before would leave base at
        # 23:48 the day before; the weekday's start is held at 00:00 instead.
        d = _driver()
        for day in _lookback(6)[:4]:                       # Sundays; Saturdays idle
            self.leg(d, day, 0, 10, MCO, MCO)
        sunday = rs.suggest_regular_shifts([d], TODAY)[d.id][6]
        self.assertEqual(sunday.template_id, self.t["morning"].id)
        # 00:10 + 75.5 + 12 = 01:37.5 -> 01:40.
        self.assertEqual((sunday.start, sunday.end), (time(0, 0), time(1, 40)))
        self.assertEqual(DayShift(6, sunday.template_id, sunday.start, sunday.end).minutes(),
                         (0, 100))

    def test_suggest_counts_todays_night_tail_for_yesterday(self):
        # Yesterday's shift ran past midnight into today: today's 01:00 pickup
        # is part of it, though today itself is never suggested from.
        d = _driver()
        sundays = _lookback(6)[:4]
        self.assertEqual(sundays[0], TODAY - timedelta(days=1))
        for day in sundays:
            self.leg(d, day, 16, 0, MCO, DISNEY)
        for day in sundays[:2]:                            # yesterday's tail is TODAY 01:00
            self.leg(d, day + timedelta(days=1), 1, 0, MCO, MCO)
        days = rs.suggest_regular_shifts([d], TODAY)[d.id]
        sunday = days[6]
        self.assertEqual((sunday.weeks_worked, sunday.template_id), (4, self.t["evening"].id))
        self.assertEqual(sunday.start, time(15, 35))       # 16:00 - 22 = 15:38 -> 15:35
        # Ends 16:00 + 75.5 + 35 = 17:50.5 (no tail) and 01:00 + 75.5 + 12 = 02:27.5
        # (tail); median of two each = 22:09 -> 22:10. Without today's tail: 17:55.
        self.assertEqual(sunday.end, time(22, 10))
        self.assertEqual(days[0].weeks_worked, 0)          # the 01:00 pickups aren't Mondays

    def test_suggest_majority_shape(self):
        # 3 Morning Mondays and 2 Midday: Morning, with medians over the Morning
        # days only (over all five they'd be 04:55 and 07:15).
        d = _driver()
        mondays = _lookback(0)
        for day, mm in zip(mondays[:3], (0, 10, 20)):
            self.leg(d, day, 5, mm, MCO, DISNEY)           # raw 04:38, 04:48, 04:58
        for day in mondays[3:5]:
            self.leg(d, day, 8, 0, MCO, DISNEY)            # raw 07:38: Midday
        monday = rs.suggest_regular_shifts([d], TODAY)[d.id][0]
        self.assertEqual((monday.weeks_worked, monday.template_id), (5, self.t["morning"].id))
        self.assertEqual(monday.start, time(4, 45))        # median 04:48 -> 04:45
        # Ends 05:10 + 75.5 + 35 = 07:00.5 (the median) -> 07:05.
        self.assertEqual(monday.end, time(7, 5))

    def test_suggest_tied_shape_goes_to_nearest_median_start(self):
        d = _driver()
        tuesdays, wednesdays = _lookback(1), _lookback(2)
        for day in tuesdays[:2]:
            self.leg(d, day, 5, 0, MCO, DISNEY)            # raw 04:38: Morning
        for day in tuesdays[2:4]:
            self.leg(d, day, 8, 0, MCO, DISNEY)            # raw 07:38: Midday
        for day in wednesdays[:2]:
            self.leg(d, day, 3, 42, MCO, DISNEY)           # raw 03:20: Morning
        for day in wednesdays[2:4]:
            self.leg(d, day, 6, 32, MCO, DISNEY)           # raw 06:10: Midday
        days = rs.suggest_regular_shifts([d], TODAY)[d.id]
        tuesday, wednesday = days[1], days[2]
        # Tuesday 2-2: median raw 06:08 sits in Midday's band, so Midday (not the
        # first shape), with Midday-only medians: 07:38 -> 07:35, 08:00 + 75.5 + 35
        # = 09:50.5 -> 09:55.
        self.assertEqual((tuesday.weeks_worked, tuesday.template_id), (4, self.t["midday"].id))
        self.assertEqual((tuesday.start, tuesday.end), (time(7, 35), time(9, 55)))
        # Wednesday 2-2: median raw 04:45 sits in Morning's band, so Morning:
        # 03:20; 03:42 + 75.5 + 35 = 05:32.5 -> 05:35.
        self.assertEqual((wednesday.weeks_worked, wednesday.template_id),
                         (4, self.t["morning"].id))
        self.assertEqual((wednesday.start, wednesday.end), (time(3, 20), time(5, 35)))

    def test_suggest_lookback_includes_its_first_day(self):
        d = _driver()
        oldest = _lookback(0)[-4:]
        self.assertEqual(oldest[-1], TODAY - timedelta(days=rs.LOOKBACK_DAYS))
        for day in oldest:
            self.leg(d, day, 5, 0, MCO, DISNEY)
        self.leg(d, oldest[-1] - timedelta(days=7), 5, 0, MCO, DISNEY)    # a week too old
        self.leg(d, oldest[-1] - timedelta(days=1), 10, 0, MCO, DISNEY)   # the day before
        days = rs.suggest_regular_shifts([d], TODAY)[d.id]
        self.assertEqual((days[0].weeks_worked, days[0].template_id), (4, self.t["morning"].id))
        self.assertEqual(days[6].weeks_worked, 0)

    def test_suggest_end_capped_at_template_span(self):
        d = _driver()
        for day in _lookback(5)[:4]:                       # Saturdays
            self.leg(d, day, 5, 0, MCO, DISNEY)
            self.leg(d, day, 16, 0, MCO, DISNEY)
        saturday = rs.suggest_regular_shifts([d], TODAY)[d.id][5]
        self.assertEqual(saturday.template_id, self.t["morning"].id)
        self.assertEqual(saturday.start, time(4, 35))
        # 16:00 + 75.5 + 35 = 17:50.5 -> 17:55 would be 13h 20m; capped at 04:35 + 12h.
        self.assertEqual(saturday.end, time(16, 35))

    def test_suggest_no_recent_trips(self):
        # Review Focus 4: nothing in 8 weeks -> Off every day, and it still confirms.
        never = _driver("Nev")
        old = _driver("Old")
        self.leg(old, TODAY - timedelta(days=rs.LOOKBACK_DAYS + 1), 5, 0)
        self.leg(old, TODAY, 5, 0)                         # today is outside [today - 56d, today)
        got = rs.suggest_regular_shifts([never, old], TODAY)
        for d in (never, old):
            self.assertEqual([s.day for s in got[d.id]], list(range(7)))
            self.assertTrue(all(s.weeks_worked == 0 and s.template_id is None
                                and s.start is None and s.end is None for s in got[d.id]))
        days = [DayShift(s.day, s.template_id, s.start, s.end) for s in got[never.id]]
        rs.save_regular_shift(never, days, self.manager)
        never.refresh_from_db()
        self.assertTrue(never.has_regular_shift)
        self.assertEqual(rs.summary_label(rs.current_days(never), rs.templates_by_id()),
                         "Off every day")

    def test_suggest_ignores_cancelled_legs(self):
        d = _driver()
        for day in _lookback(0)[:5]:
            self.leg(d, day, 5, 0, status="cancelled")
        for day in _lookback(1)[:5]:
            self.leg(d, day, 5, 0, res_status="canceled")
        for day in _lookback(2)[:5]:
            self.leg(d, day, 5, 0, res_status="cancelled")
        for day in _lookback(3)[:5]:
            self.leg(d, day, 5, 0, status="completed")     # completed counts
        days = rs.suggest_regular_shifts([d], TODAY)[d.id]
        self.assertEqual([s.weeks_worked for s in days], [0, 0, 0, 5, 0, 0, 0])

    def test_suggest_without_any_shapes_still_counts_weeks(self):
        d = _driver()
        for day in _lookback(0)[:5]:
            self.leg(d, day, 5, 0)
        ShiftTemplate.objects.all().delete()
        rs.clear_template_cache()
        monday = rs.suggest_regular_shifts([d], TODAY)[d.id][0]
        self.assertEqual((monday.weeks_worked, monday.template_id, monday.start), (5, None, None))

    def test_suggest_one_query(self):
        drivers = [_driver(f"D{i}") for i in range(5)]
        for d in drivers:
            for day in _lookback(0)[:4]:
                self.leg(d, day, 5, 0)
        rs.clear_template_cache()
        with self.assertNumQueries(2):                     # the templates + one Leg query
            got = rs.suggest_regular_shifts(drivers, TODAY)
        self.assertEqual(set(got), {d.id for d in drivers})
        self.assertTrue(all(got[d.id][0].template_id == self.t["morning"].id for d in drivers))

    def test_suggest_never_picks_float(self):
        # Float's start band covers the whole day, so it would be "nearest" to a
        # start between two shapes; a suggestion is always a fixed shape and
        # Float is the manager's call.
        d = _driver()
        for day in _lookback(0)[:4]:
            self.leg(d, day, 10, 22, MCO, DISNEY)          # raw 10:00: 1h past Midday
        monday = rs.suggest_regular_shifts([d], TODAY)[d.id][0]
        self.assertEqual(monday.template_id, self.t["midday"].id)
        self.assertEqual(monday.start, time(10, 0))


# ════════════════════════════════════════════════════════════════════════════
# Validation, band warnings and the band fill
# ════════════════════════════════════════════════════════════════════════════

class ValidateTests(_Fixture):
    def test_validate_messages(self):
        self.assertEqual(self.validate(self.week(mon=("morning", time(4, 10), None))),
                         ["Monday: pick a start and an end time, or set the day to Off."])
        self.assertEqual(self.validate(self.week(tue=("morning", time(4, 0), time(17, 0)))),
                         ["Tuesday: a shift longer than 12 hours isn't allowed."])
        short = copy.copy(self.t["morning"])
        short.max_span_minutes = 690
        self.assertEqual(self.validate(self.week(tue=("morning", time(4, 0), time(16, 0))),
                                       templates={**rs.templates_by_id(), short.id: short}),
                         ["Tuesday: a shift longer than 11.5 hours isn't allowed."])
        self.assertEqual(self.validate(self.week(wed=("morning", time(4, 10), time(15, 30))),
                                       hard_earliest_start=time(5)),
                         ["Wednesday: starts at 4:10 AM — before this driver's earliest start (5 AM)."])
        self.assertEqual(self.validate(self.week(thu=("evening", time(14, 15), time(2, 15))),
                                       hard_latest_finish=time(23)),
                         ["Thursday: ends at 2:15 AM — after this driver's latest finish (11 PM)."])
        six = self.week(**{k: ("morning", time(4, 10), time(15, 30)) for k in _DAY_KEYS[:6]})
        self.assertEqual(self.validate(six, max_days_per_week=5),
                         ["6 working days — more than this driver's limit of 5 a week."])
        self.assertEqual(self.validate(self.week(sun=("evening", time(14, 0), time(2, 0)),
                                                 mon=("morning", time(8, 0), time(17, 0)))),
                         ["Sunday to Monday: only 6h 0m off between shifts; the minimum is 8h 30m."])

    def test_validate_latest_finish_next_day(self):
        evening = self.week(thu=("evening", time(14, 15), time(2, 15)))
        self.assertEqual(self.validate(evening, hard_latest_finish=time(1),
                                       hard_latest_finish_next_day=True),
                         ["Thursday: ends at 2:15 AM — after this driver's latest finish (1 AM)."])
        self.assertEqual(self.validate(evening, hard_latest_finish=time(2, 15),
                                       hard_latest_finish_next_day=True), [])
        morning = self.week(thu=("morning", time(4, 10), time(15, 30)))
        self.assertEqual(self.validate(morning, hard_latest_finish=time(1),
                                       hard_latest_finish_next_day=True), [])

    def test_validate_rest_between_days_and_zero_disables(self):
        days = self.week(mon=("evening", time(14, 0), time(2, 0)),
                         tue=("morning", time(9, 0), time(18, 0)))
        self.assertEqual(self.validate(days),
                         ["Monday to Tuesday: only 7h 0m off between shifts; the minimum is 8h 30m."])
        self.assertEqual(self.validate(days, rest_min=0), [])
        self.assertEqual(self.validate(days, rest_min=420), [])
        overlap = self.week(mon=("evening", time(14, 0), time(2, 0)),
                            tue=("morning", time(1, 0), time(10, 0)))  # starts before Monday ends
        self.assertEqual(self.validate(overlap),
                         ["Monday to Tuesday: only 0h 0m off between shifts; the minimum is 8h 30m."])

    def test_validate_unknown_shape(self):
        days = [DayShift(0, 99999, time(4, 10), time(15, 30)), DayShift(1, 99999, None, None),
                DayShift(2, self.t["morning"].id, None, None, alt_template_id=99999)]
        self.assertEqual(self.validate(days), [
            "Monday: pick Morning, Midday, Evening or Float, or set the day to Off.",
            "Tuesday: pick Morning, Midday, Evening or Float, or set the day to Off.",
            "Wednesday: pick Morning, Midday, Evening or Float, or set the day to Off.",
        ])

    def test_validate_messages_come_in_day_order(self):
        days = self.week(mon=("morning", time(4, 0), time(17, 0)),
                         wed=("morning", time(4, 10), None),
                         fri=("morning", time(4, 10), time(15, 30)))
        self.assertEqual(self.validate(days, hard_earliest_start=time(4, 30), max_days_per_week=2), [
            "Monday: a shift longer than 12 hours isn't allowed.",
            "Monday: starts at 4 AM — before this driver's earliest start (4:30 AM).",
            "Wednesday: pick a start and an end time, or set the day to Off.",
            "Friday: starts at 4:10 AM — before this driver's earliest start (4:30 AM).",
            "3 working days — more than this driver's limit of 2 a week.",
        ])

    def test_limit_messages_are_the_three_limit_checks_only(self):
        days = self.week(mon=("morning", time(4, 0), time(17, 0)),      # too long: not a limit
                         tue=("evening", time(14, 15), time(2, 15)),
                         wed=("morning", time(4, 10), None),            # no end: not a limit
                         sun=("evening", time(14, 0), time(2, 0)))      # short rest: not a limit
        self.assertEqual(rs.limit_messages(days, hard_earliest_start=time(4, 30),
                                           hard_latest_finish=time(23),
                                           hard_latest_finish_next_day=False,
                                           max_days_per_week=2), [
            "Monday: starts at 4 AM — before this driver's earliest start (4:30 AM).",
            "Tuesday: ends at 2:15 AM — after this driver's latest finish (11 PM).",
            "Sunday: ends at 2 AM — after this driver's latest finish (11 PM).",
            "4 working days — more than this driver's limit of 2 a week.",
        ])
        self.assertEqual(rs.limit_messages(days, **_NO_LIMITS), [])

    def test_band_fill_is_valid_for_every_seeded_shape(self):
        fills = {kind: rs.band_fill(t) for kind, t in self.t.items()}
        self.assertEqual(fills, {"morning": (time(6), time(16)),
                                 "midday": (time(9), time(21)),
                                 "evening": (time(16), time(2, 15)),
                                 "float": (time(16), time(2, 15))})
        for kind, (start, end) in fills.items():
            with self.subTest(kind=kind):
                days = self.week(**{k: (kind, start, end) for k in _DAY_KEYS})
                self.assertEqual(self.validate(days), [])
                self.assertEqual(rs.band_warnings(days, rs.templates_by_id()), [])

    def test_band_fill_end_band_after_midnight(self):
        late = copy.copy(self.t["evening"])
        late.start_latest, late.end_earliest, late.end_latest = time(17), time(1), time(2)
        self.assertEqual(rs.band_fill(late), (time(17), time(2)))
        late.max_span_minutes = 480
        self.assertEqual(rs.band_fill(late), (time(17), time(1)))

    def test_band_warning(self):
        days = self.week(mon=("morning", time(2, 0), time(11, 0)),
                         tue=("morning", time(4, 10), time(15, 30)),
                         wed=("evening", time(14, 15), time(2, 15)),
                         thu=("midday", time(7, 0), time(23, 30)))
        self.assertEqual(rs.band_warnings(days, rs.templates_by_id()), [
            "Monday: 2 AM–11 AM is outside the usual Morning shape "
            "(leaves 3 AM–6 AM, back 12 PM–4 PM).",
            "Thursday: 7 AM–11:30 PM is outside the usual Midday shape "
            "(leaves 6 AM–9 AM, back 3 PM–9 PM).",
        ])


# ════════════════════════════════════════════════════════════════════════════
# Saving, and the two legacy editors (Review Focus 1 and 2)
# ════════════════════════════════════════════════════════════════════════════

_LEGACY_KEYS = ("is_available", "shift_type", "start_hour", "end_hour", "flexible", "max_hours",
                "preferred_shift", "preference", "status", "display_label", "tooltip")
# The keys Task 4 adds to describe the regular shift itself. Everything else the
# resolver returns is the legacy reading, and confirming may not move any of it.
_REGULAR_EFF_KEYS = frozenset({
    "regular_shift", "regular_day_off", "hard_earliest_start", "hard_latest_finish",
    "hard_latest_finish_next_day", "window_start_min", "window_end_min", "window_kind",
    "window_max_span_min", "shift_role_label"})


class SaveTests(_Fixture):
    def _legacy(self, driver_id):
        driver = Driver.objects.get(pk=driver_id)
        out = []
        for i in range(7):
            eff = resolve_effective_availability(driver, TODAY + timedelta(days=i))
            self.assertLessEqual(set(_LEGACY_KEYS), set(eff))
            out.append({k: v for k, v in eff.items() if k not in _REGULAR_EFF_KEYS})
        return out

    def test_save_refuses_invalid(self):
        d = _driver()
        with self.assertRaises(ValueError):
            rs.save_regular_shift(d, self.week(mon=("morning", time(4, 0), time(17, 0))),
                                  self.manager)
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=d).exists())
        d.refresh_from_db()
        self.assertIsNone(d.regular_shift_confirmed_at)

    def test_save_refuses_a_day_outside_the_drivers_limits(self):
        d = _driver(hard_earliest_start=time(5))
        with self.assertRaises(ValueError) as cm:
            rs.save_regular_shift(d, self.week(mon=("morning", time(4, 10), time(15, 30))),
                                  self.manager)
        self.assertIn("Monday: starts at 4:10 AM", str(cm.exception))
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=d).exists())

    def test_save_refuses_an_unknown_shape(self):
        d = _driver()
        with self.assertRaises(ValueError) as cm:
            rs.save_regular_shift(d, [DayShift(0, 99999, time(4, 10), time(15, 30))],
                                  self.manager)
        self.assertEqual(str(cm.exception),
                         "Monday: pick Morning, Midday, Evening or Float, or set the day to Off.")
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=d).exists())
        d.refresh_from_db()
        self.assertIsNone(d.regular_shift_confirmed_at)

    def test_save_refreshes_a_prefetched_driver(self):
        d = _driver()
        rs.save_regular_shift(d, self.week(mon=("morning", time(4, 10), time(15, 30))),
                              self.manager)
        fetched = Driver.objects.prefetch_related("weekly_schedule").get(pk=d.pk)
        self.assertEqual(rs.current_days(fetched)[0].template_id, self.t["morning"].id)
        days = self.week(tue=("midday", time(7), time(19)))
        rs.save_regular_shift(fetched, days, self.manager)
        self.assertEqual(rs.current_days(fetched), days)

    def test_save_creates_rows_with_legacy_defaults_parity(self):
        d = _driver(default_start_hour=5, default_end_hour=19, default_flexible=False,
                    default_shift_type="custom", default_max_hours=Decimal("10.5"),
                    default_preferred_shift="morning", default_preference="prefer_arrival")
        # One weekday already has a legacy row; the other six fall back to defaults.
        DriverWeeklySchedule.objects.create(driver=d, day_of_week=2, is_available=False,
                                            start_hour=8, end_hour=14, flexible=True,
                                            scheduling_notes="school run")
        before = self._legacy(d.id)
        days = self.week(**{k: ("morning", time(4, 10), time(15, 30)) for k in _DAY_KEYS[:5]},
                         sat=("evening", time(14, 15), time(2, 15)))
        rs.save_regular_shift(d, days, self.manager)
        self.assertEqual(self._legacy(d.id), before)

        rows = {r.day_of_week: r for r in DriverWeeklySchedule.objects.filter(driver=d)}
        self.assertEqual(sorted(rows), [0, 1, 2, 3, 4, 5])     # no row made for an Off day
        wed = rows[2]
        self.assertEqual((wed.is_available, wed.start_hour, wed.end_hour, wed.flexible,
                          wed.scheduling_notes), (False, 8, 14, True, "school run"))
        self.assertEqual((wed.shift_template_id, wed.shift_start, wed.shift_end),
                         (self.t["morning"].id, time(4, 10), time(15, 30)))
        mon = rows[0]
        self.assertEqual((mon.is_available, mon.shift_type, mon.start_hour, mon.end_hour,
                          mon.flexible, mon.max_hours, mon.preferred_shift, mon.preference),
                         (True, "custom", 5, 19, False, Decimal("10.5"), "morning",
                          "prefer_arrival"))
        self.assertEqual(rows[5].regular_minutes(), (855, 1575))
        d.refresh_from_db()
        self.assertIsNotNone(d.regular_shift_confirmed_at)
        self.assertEqual(d.regular_shift_confirmed_by, self.manager)

    def test_save_off_day_clears_an_earlier_shift(self):
        d = _driver()
        rs.save_regular_shift(d, self.week(mon=("morning", time(4, 10), time(15, 30))),
                              self.manager)
        rs.save_regular_shift(d, self.week(), self.manager)
        row = DriverWeeklySchedule.objects.get(driver=d, day_of_week=0)
        self.assertEqual((row.shift_template_id, row.shift_start, row.shift_end),
                         (None, None, None))
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk)), self.week())

    def test_current_days(self):
        d = _driver()
        self.assertEqual(rs.current_days(d), self.week())
        days = self.week(tue=("midday", time(7), time(19)),
                         sun=("evening", time(14, 15), time(2, 15)))
        rs.save_regular_shift(d, days, self.manager)
        got = rs.current_days(Driver.objects.get(pk=d.pk))
        self.assertEqual(got, days)
        self.assertEqual(got[6].minutes(), (855, 1575))
        self.assertIsNone(got[0].minutes())

    def test_legacy_save_keeps_regular_fields(self):
        # Review Focus 1: the planner's Driver Schedules modal and the Edit
        # Schedules drawer both post here; neither may touch a regular shift.
        d = _driver()
        days = self.week(mon=("morning", time(4, 10), time(15, 30), {"day_latest": time(15, 30)}),
                         wed=("midday", None, None, {"alt": "evening"}),
                         fri=("evening", time(14, 15), time(2, 15),
                              {"day_latest": time(2, 15), "day_latest_next_day": True}))
        rs.save_regular_shift(d, days, self.manager, role_template_id=self.t["morning"].id)
        self.client.force_login(self.dispatcher)
        modal = {"drivers": [{
            "id": d.id, "default_start_hour": 6, "default_end_hour": 23,
            "default_flexible": True, "default_preference": "",
            "weekly": {str(i): {"is_available": True, "start_hour": 7, "end_hour": 18,
                                "flexible": False, "preference": ""} for i in range(7)},
        }]}
        drawer = {"drivers": [{
            "id": d.id, "default_start_hour": 6, "default_end_hour": 23,
            "default_flexible": True, "default_shift_type": "full_day",
            "default_max_hours": None, "default_preferred_shift": "", "default_preference": "",
            "notes": "",
            "weekly": {str(i): {"is_available": i != 3, "shift_type": "custom", "start_hour": 8,
                                "end_hour": 17, "flexible": False, "max_hours": None,
                                "preferred_shift": "", "preference": "",
                                "scheduling_notes": ""} for i in range(7)},
        }]}
        for name, payload, start_hour in (("modal", modal, 7), ("drawer", drawer, 8)):
            with self.subTest(editor=name):
                resp = self.client.post(reverse("save_driver_weekly_schedules"),
                                        data=json.dumps(payload),
                                        content_type="application/json")
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.json()["success"])
                rows = {r.day_of_week: r for r in DriverWeeklySchedule.objects.filter(driver=d)}
                self.assertEqual(rows[0].start_hour, start_hour)       # the legacy edit landed
                self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk)), days)
        d.refresh_from_db()
        self.assertTrue(d.has_regular_shift)
        self.assertEqual(d.shift_role_id, self.t["morning"].id)


# ════════════════════════════════════════════════════════════════════════════
# The roster and the switch (S3)
# ════════════════════════════════════════════════════════════════════════════

class RosterAndSwitchTests(_Fixture):
    def _confirm_off(self, driver):
        rs.save_regular_shift(driver, self.week(), self.manager)

    def _switch_in_db(self):
        return SchedulerSettings.objects.get(pk=1).regular_shift_windows

    def test_roster_excludes_placeholder_and_affiliates(self):
        # The placeholder record is left out by id (production's is 6); point
        # Day Setup's list at this one rather than forcing a primary key.
        placeholder = _driver("Unassigned", "Pool")
        zed = _driver("Zed", "Zulu")
        amy = _driver("Amy", "Alpha")
        _driver("Aff", "Iliate", driver_type="affiliate")
        _driver("Gone", "Away", is_active=False)
        _driver("Op", "Erator", portal_role="operator")
        _driver("Demo", "Account")
        with mock.patch.object(day_setup, "DAY_SETUP_EXCLUDE_DRIVER_IDS", {placeholder.id}):
            self.assertEqual(rs.roster_drivers(), [amy, zed])
        self.assertEqual(rs.roster_drivers(), [amy, placeholder, zed])   # only its id kept it out

    def test_drivers_without_regular_shift(self):
        amy, bob = _driver("Amy", "Alpha"), _driver("Bob", "Bravo")
        self._confirm_off(amy)
        self.assertEqual(rs.drivers_without_regular_shift(), [bob])

    def test_switch_refused_until_list_empty(self):
        abe, bea, cal, dee = (_driver("Abe", "Adams"), _driver("Bea", "Brown"),
                              _driver("Cal", "Clark"), _driver("Dee", "Dunn"))
        _driver("Aff", "Iliate", driver_type="affiliate")         # never blocks the switch
        self.assertEqual(rs.set_regular_windows(True, self.manager),
                         (False, "4 drivers still need a regular shift: "
                                 "Abe Adams, Bea Brown, Cal Clark…"))
        self._confirm_off(dee)
        self.assertEqual(rs.set_regular_windows(True, self.manager),
                         (False, "3 drivers still need a regular shift: "
                                 "Abe Adams, Bea Brown, Cal Clark"))
        self._confirm_off(abe)
        self._confirm_off(bea)
        self.assertEqual(rs.set_regular_windows(True, self.manager),
                         (False, "1 driver still needs a regular shift: Cal Clark"))
        self.assertFalse(self._switch_in_db())
        self.assertFalse(rs.regular_windows_on())

    def test_switch_on_when_all_confirmed(self):
        # A week where every day is Off is a confirmed regular shift (S2).
        self._confirm_off(_driver("Amy", "Alpha"))
        with self.assertLogs("drivers.regular_shifts", "INFO") as logs:
            self.assertEqual(rs.set_regular_windows(True, self.manager), (True, ""))
        self.assertIn("rs_manager", logs.output[0])
        self.assertTrue(self._switch_in_db())
        self.assertTrue(rs.regular_windows_on())

    def test_switch_off_always_allowed(self):
        _driver("Amy", "Alpha")                                   # unconfirmed
        SchedulerSettings.objects.update_or_create(pk=1, defaults={"regular_shift_windows": True})
        self.assertEqual(rs.set_regular_windows(False, self.manager), (True, ""))
        self.assertFalse(self._switch_in_db())
        self.assertFalse(rs.regular_windows_on())

    def test_switch_cache_cleared_on_set(self):
        self.assertFalse(rs.regular_windows_on())
        self.assertIs(cache.get(REGULAR_WINDOWS_CACHE_KEY), False)
        stale = SchedulerSettings.get_settings()                  # the process-cached row
        self.assertFalse(stale.regular_shift_windows)
        self.assertEqual(rs.set_regular_windows(True, self.manager), (True, ""))
        self.assertTrue(rs.regular_windows_on())
        self.assertTrue(SchedulerSettings.get_settings().regular_shift_windows)
        self.assertEqual(rs.set_regular_windows(False, self.manager), (True, ""))
        self.assertFalse(rs.regular_windows_on())

    def test_switch_read_is_cached_for_a_minute(self):
        with self.assertNumQueries(1):
            self.assertFalse(rs.regular_windows_on())
        SchedulerSettings.objects.update_or_create(pk=1, defaults={"regular_shift_windows": True})
        with self.assertNumQueries(0):
            self.assertFalse(rs.regular_windows_on())             # until the key goes
        cache.delete(REGULAR_WINDOWS_CACHE_KEY)
        self.assertTrue(rs.regular_windows_on())

    def test_switch_missing_settings_row_reads_off(self):
        SchedulerSettings.objects.all().delete()
        self.assertFalse(rs.regular_windows_on())

    def test_templates_cached(self):
        rs.clear_template_cache()
        with self.assertNumQueries(1):
            first = rs.templates_by_id()
        with self.assertNumQueries(0):
            self.assertEqual(rs.templates_by_id(), first)
        self.assertEqual({t.kind for t in first.values()},
                         {"morning", "midday", "evening", "float"})
        rs.clear_template_cache()
        with self.assertNumQueries(1):
            rs.templates_by_id()


# ════════════════════════════════════════════════════════════════════════════
# Labels
# ════════════════════════════════════════════════════════════════════════════

class LabelTests(_Fixture):
    def test_day_label(self):
        tpl = rs.templates_by_id()
        days = self.week(mon=("morning", time(4, 10), time(15, 30)),
                         sat=("evening", time(14, 15), time(2, 15)))
        self.assertEqual(rs.day_label(days[0], tpl), "Morning 4:10 AM – 3:30 PM")
        self.assertEqual(rs.day_label(days[5], tpl), "Evening 2:15 PM – 2:15 AM")
        self.assertEqual(rs.day_label(days[6], tpl), "Off")

    def test_summary_label(self):
        tpl = rs.templates_by_id()
        days = self.week(**{k: ("morning", time(4, 10), time(15, 30)) for k in _DAY_KEYS[:5]},
                         sat=("evening", time(14, 15), time(2, 15)))
        self.assertEqual(rs.summary_label(days, tpl),
                         "Mon–Fri Morning 4:10 AM–3:30 PM · Sat Evening 2:15 PM–2:15 AM")
        split = self.week(mon=("morning", time(4, 10), time(15, 30)),
                          tue=("morning", time(4, 10), time(15, 30)),
                          wed=("morning", time(5, 0), time(15, 30)),
                          fri=("morning", time(5, 0), time(15, 30)))
        self.assertEqual(rs.summary_label(split, tpl),
                         "Mon–Tue Morning 4:10 AM–3:30 PM · Wed Morning 5 AM–3:30 PM"
                         " · Fri Morning 5 AM–3:30 PM")
        self.assertEqual(rs.summary_label(self.week(), tpl), "Off every day")

    def test_summary_label_reads_suggestions(self):
        d = _driver()
        for day in _lookback(0)[:5]:
            self.leg(d, day, 5, 0, MCO, DISNEY)
            self.leg(d, day, 9, 0, DISNEY, MCO)
        suggestion = rs.suggest_regular_shifts([d], TODAY)[d.id]
        # As in test_suggest_regular_weekday: 09:00 + 34.8 + 12 (MCO -> base) -> 9:50 AM.
        self.assertEqual(rs.summary_label(suggestion, rs.templates_by_id()),
                         "Mon Morning 4:35 AM–9:50 AM")


# ════════════════════════════════════════════════════════════════════════════
# Task 3b: the usual shift, Float, and per-day options (S15–S18)
# ════════════════════════════════════════════════════════════════════════════

_LEGACY_ROW_FIELDS = ("is_available", "shift_type", "start_hour", "end_hour", "flexible",
                      "max_hours", "preferred_shift", "preference", "scheduling_notes")
_NO_HARD = dict(hard_lo=None, hard_hi=None)


class UsualShiftTests(_Fixture):
    def test_float_template_seeded(self):
        f = ShiftTemplate.objects.get(kind="float")
        self.assertEqual((f.name, f.start_earliest, f.start_latest, f.end_earliest,
                          f.end_latest, f.max_span_minutes, f.sort_order),
                         ("Float", time(3), time(16), time(12), time(2, 15), 720, 4))
        self.assertEqual(f.notes, "Any shape — goes wherever the day needs him, still "
                                  "within 12 hours and his limits.")
        self.assertEqual(f.get_kind_display(), "Float")
        self.assertEqual(f.end_band_minutes(), (720, 1575))
        self.assertEqual([t.kind for t in sorted(rs.templates_by_id().values(),
                                                 key=lambda t: t.sort_order)],
                         ["morning", "midday", "evening", "float"])

    def test_role_saved_and_labelled(self):
        d = _driver()
        tpl = rs.templates_by_id()
        self.assertEqual(rs.role_label(d, tpl), "")
        rs.save_regular_shift(d, self.week(mon=("morning", time(4, 10), time(15, 30))),
                              self.manager, role_template_id=self.t["morning"].id)
        d = Driver.objects.get(pk=d.pk)
        self.assertEqual(d.shift_role_id, self.t["morning"].id)
        with self.assertNumQueries(0):                 # reads shift_role_id, never the row
            self.assertEqual(rs.role_label(d, tpl), "Morning driver")
        for kind, label in (("midday", "Midday driver"), ("evening", "Evening driver"),
                            ("float", "Float — any shift")):
            d.shift_role_id = self.t[kind].id
            self.assertEqual(rs.role_label(d, tpl), label)
        d.shift_role_id = 99999
        self.assertEqual(rs.role_label(d, tpl), "")
        # The role is the default and the label; the days stay the source of truth.
        rs.save_regular_shift(Driver.objects.get(pk=d.pk), self.week(), self.manager)
        self.assertIsNone(Driver.objects.get(pk=d.pk).shift_role_id)

    def test_save_refuses_an_unknown_role(self):
        d = _driver()
        with self.assertRaises(ValueError) as cm:
            rs.save_regular_shift(d, self.week(mon=("morning", None, None)), self.manager,
                                  role_template_id=99999)
        self.assertEqual(str(cm.exception),
                         "Pick Morning, Midday, Evening or Float as the usual shift.")
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=d).exists())
        d.refresh_from_db()
        self.assertIsNone(d.regular_shift_confirmed_at)

    def test_suggest_role_most_common(self):
        S = rs.DaySuggestion
        tpl = rs.templates_by_id()
        m, mid, e = self.t["morning"].id, self.t["midday"].id, self.t["evening"].id
        off = [S(i, 0, None, None, None) for i in range(7)]
        mostly_evening = [S(0, 5, m, time(4, 35), time(15)), S(1, 6, e, time(14), time(2)),
                          S(2, 6, e, time(14), time(2)), S(3, 4, mid, time(7), time(19)),
                          S(4, 2, None, None, None)]
        self.assertEqual(rs.suggest_role(mostly_evening, tpl), e)
        tie = [S(0, 4, e, time(14), time(2)), S(1, 4, m, time(4), time(15))]
        self.assertEqual(rs.suggest_role(tie, tpl), m)         # the lower sort_order
        self.assertIsNone(rs.suggest_role(off, tpl))
        self.assertIsNone(rs.suggest_role([], tpl))

    def test_suggest_role_from_real_suggestions(self):
        d = _driver()
        for day in _lookback(0)[:5] + _lookback(1)[:5]:
            self.leg(d, day, 5, 0, MCO, DISNEY)
        for day in _lookback(4)[:4]:
            self.leg(d, day, 16, 0, MCO, DISNEY)
        suggestion = rs.suggest_regular_shifts([d], TODAY)[d.id]
        self.assertEqual(rs.suggest_role(suggestion, rs.templates_by_id()), self.t["morning"].id)


class DayOptionTests(_Fixture):
    def test_blank_times_use_band_fill(self):
        tpl = rs.templates_by_id()
        days = self.week(mon=("morning", None, None), wed=("midday", None, None),
                         fri=("evening", None, None), sun=("float", None, None))
        self.assertEqual(self.validate(days), [])
        self.assertEqual([rs.effective_minutes(day, tpl) for day in days],
                         [(360, 960), None, (540, 1260), None, (960, 1575), None, (960, 1575)])
        typed = self.week(mon=("morning", time(4, 10), time(15, 30)))[0]
        self.assertEqual(rs.effective_minutes(typed, tpl), (250, 930))
        self.assertEqual(rs.regular_window(days[0], tpl, **_NO_HARD),
                         rs.RegularWindow(360, 1080, "morning", 720))
        self.assertEqual(rs.regular_window(days[2], tpl, **_NO_HARD),
                         rs.RegularWindow(540, 1260, "midday", 720))
        self.assertEqual(rs.regular_window(days[4], tpl, **_NO_HARD),
                         rs.RegularWindow(855, 1575, "evening", 720))
        self.assertEqual(rs.band_warnings(days, tpl), [])
        d = _driver()
        rs.save_regular_shift(d, days, self.manager)
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk)), days)

    def test_one_time_blank_refused(self):
        days = self.week(mon=("morning", None, time(15, 30)), tue=("float", time(5), None),
                         wed=("morning", None, None, {"alt": "evening"}))
        self.assertEqual(self.validate(days), [
            "Monday: pick a start and an end time, or set the day to Off.",
            "Tuesday: pick a start and an end time, or set the day to Off.",
        ])
        d = _driver()
        with self.assertRaises(ValueError):
            rs.save_regular_shift(d, days, self.manager)
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=d).exists())

    def test_alt_shape_must_differ(self):
        self.assertEqual(self.validate(self.week(mon=("morning", None, None, {"alt": "morning"}))),
                         ["Monday: the second shift must be different from the first."])
        self.assertEqual(self.validate(self.week(mon=("morning", None, None, {"alt": "evening"}))),
                         [])

    def test_float_cannot_have_alt(self):
        days = self.week(tue=("float", None, None, {"alt": "evening"}),
                         wed=("morning", None, None, {"alt": "float"}))
        self.assertEqual(self.validate(days), [
            "Tuesday: Float already covers every shift — no second shift needed.",
            "Wednesday: Float already covers every shift — no second shift needed.",
        ])

    def test_day_limit_messages(self):
        thu = self.week(thu=("morning", time(4, 10), time(15, 30), {"day_latest": time(15)}))
        msg = ["Thursday: ends at 3:30 PM — after that day's finish-by (3 PM)."]
        self.assertEqual(self.validate(thu), msg)
        self.assertEqual(rs.limit_messages(thu, **_NO_LIMITS), msg)
        self.assertEqual(
            self.validate(self.week(fri=("morning", time(4, 10), time(15, 30),
                                         {"day_earliest": time(5)}))),
            ["Friday: starts at 4:10 AM — before that day's earliest start (5 AM)."])
        late = {"day_latest_next_day": True}
        self.assertEqual(
            self.validate(self.week(sat=("evening", time(14, 15), time(2, 15),
                                         {**late, "day_latest": time(1)}))),
            ["Saturday: ends at 2:15 AM — after that day's finish-by (1 AM)."])
        self.assertEqual(
            self.validate(self.week(sat=("evening", time(14, 15), time(2, 15),
                                         {**late, "day_latest": time(2, 30)}))), [])
        self.assertEqual(
            self.validate(self.week(sat=("evening", time(14, 15), time(2, 15),
                                         {"day_latest": time(23)}))),
            ["Saturday: ends at 2:15 AM — after that day's finish-by (11 PM)."])
        # The driver's own limit and the day's both apply, the driver's first.
        self.assertEqual(
            self.validate(self.week(mon=("morning", time(4, 10), time(15, 30),
                                         {"day_earliest": time(4, 30)})),
                          hard_earliest_start=time(5)),
            ["Monday: starts at 4:10 AM — before this driver's earliest start (5 AM).",
             "Monday: starts at 4:10 AM — before that day's earliest start (4:30 AM)."])
        # Usual times are clipped by the day's limit, not refused by it.
        self.assertEqual(self.validate(self.week(thu=("morning", None, None,
                                                      {"day_latest": time(15)}))), [])

    def test_limits_that_leave_no_time_are_refused(self):
        forgot_next_day = self.week(mon=("float", None, None, {"day_latest": time(2)}))
        msg = ["Monday: the start and finish limits leave no time for a shift."]
        self.assertEqual(self.validate(forgot_next_day), msg)
        self.assertEqual(rs.limit_messages(forgot_next_day, **_NO_LIMITS), msg)
        self.assertEqual(self.validate(self.week(tue=("morning", None, None)),
                                       hard_latest_finish=time(5)),
                         ["Tuesday: the start and finish limits leave no time for a shift."])
        # A typed time already refused for the same limit says so only once.
        self.assertEqual(
            self.validate(self.week(wed=("morning", time(4, 10), time(15, 30),
                                         {"day_earliest": time(17)}))),
            ["Wednesday: starts at 4:10 AM — before that day's earliest start (5 PM)."])

    def test_limits_clip_blank_and_open_days_with_a_warning(self):
        # Blank times and open days were never checked against the limits: the
        # limits clip them (S18), the week still saves, and a warning says what
        # is left — so "Evening, done by 3 PM" can't slip through unnoticed.
        W = rs.RegularWindow
        tpl = rs.templates_by_id()
        days = self.week(mon=("morning", None, None, {"day_earliest": time(15)}),
                         tue=("float", None, None),
                         wed=("morning", None, None, {"day_latest": time(15)}),
                         thu=("evening", None, None, {"day_latest": time(15)}),
                         fri=("morning", None, None, {"alt": "evening", "day_earliest": time(5)}),
                         sat=("evening", None, None,
                              {"day_latest": time(1), "day_latest_next_day": True}))
        self.assertEqual(self.validate(days), [])
        self.assertEqual(rs.regular_window(days[3], tpl, **_NO_HARD), W(855, 900, "evening", 720))
        self.assertEqual(rs.band_warnings(days, tpl), [
            "Monday: not before 3 PM limits Morning to 3 PM–6 PM.",
            "Wednesday: done by 3 PM limits Morning to 6 AM–3 PM.",
            "Thursday: done by 3 PM limits Evening to 2:15 PM–3 PM.",
            "Friday: not before 5 AM limits Morning or Evening to 5 AM–2:15 AM.",
            "Saturday: done by 1 AM (next day) limits Evening to 2:15 PM–1 AM.",
        ])
        # The driver's own limits too, when the caller passes them.
        float_day = self.week(tue=("float", None, None))
        self.assertEqual(self.validate(float_day, hard_earliest_start=time(23)), [])
        self.assertEqual(rs.band_warnings(float_day, tpl, hard_earliest_start=time(23)),
                         ["Tuesday: never starts before 11 PM limits Float to 11 PM–2:15 AM."])
        two = self.week(fri=("morning", time(9), time(17),
                             {"alt": "evening", "day_earliest": time(5)}))
        self.assertEqual(rs.band_warnings(two, tpl, hard_latest_finish=time(1),
                                          hard_latest_finish_next_day=True),
                         ["Friday: not before 5 AM and never finishes after 1 AM (next day) "
                          "limit Morning or Evening to 5 AM–1 AM."])
        # No warning when no limit bites, for typed times (validation checked
        # them), or when nothing is left (validation refuses that instead).
        quiet = self.week(mon=("morning", None, None, {"day_latest": time(19)}),
                          sun=("morning", time(4, 10), time(15, 30), {"day_latest": time(16)}))
        self.assertEqual(rs.band_warnings(quiet, tpl), [])
        self.assertEqual(rs.band_warnings(self.week(mon=("float", None, None,
                                                         {"day_latest": time(2)})), tpl), [])

    def test_open_days_typed_times_are_labels_only(self):
        # Float and two-shape days skip the typed-time span and limit checks;
        # the window and the base-to-base 12h check (Task 3c) hold them.
        days = self.week(mon=("float", time(3), time(16)),
                         tue=("morning", time(4), time(15), {"alt": "midday"}))
        self.assertEqual(self.validate(days, hard_earliest_start=time(5)), [])
        self.assertEqual(rs.band_warnings(self.week(mon=("float", time(1), time(5)),
                                                    tue=("morning", time(1), time(5),
                                                         {"alt": "evening"})),
                                          rs.templates_by_id()), [])

    def test_rest_check_uses_each_days_window(self):
        # A Morning day's end floats to start + 12h, so rest counts from there.
        self.assertEqual(self.validate(self.week(mon=("morning", time(6), time(14)),
                                                 tue=("morning", time(2), time(10)))),
                         ["Monday to Tuesday: only 8h 0m off between shifts; "
                          "the minimum is 8h 30m."])
        # ...and from the window as the day's own limits clip it.
        tue = ("morning", time(7), time(15))
        self.assertEqual(self.validate(self.week(mon=("evening", None, None), tue=tue)),
                         ["Monday to Tuesday: only 4h 45m off between shifts; "
                          "the minimum is 8h 30m."])
        self.assertEqual(self.validate(self.week(mon=("evening", None, None,
                                                      {"day_latest": time(23)}), tue=tue)),
                         ["Monday to Tuesday: only 8h 0m off between shifts; "
                          "the minimum is 8h 30m."])

    def test_rest_check_skips_float_and_two_shape_days(self):
        self.assertEqual(self.validate(self.week(mon=("float", None, None),
                                                 tue=("morning", time(3), time(12)))), [])
        self.assertEqual(self.validate(self.week(sun=("evening", time(14), time(2)),
                                                 mon=("morning", time(3), time(12),
                                                      {"alt": "midday"}))), [])

    def test_save_writes_new_fields_only(self):
        d = _driver(default_start_hour=5, default_end_hour=19, default_flexible=False)
        DriverWeeklySchedule.objects.create(driver=d, day_of_week=3, is_available=False,
                                            shift_type="custom", start_hour=8, end_hour=14,
                                            flexible=True, scheduling_notes="school run")

        def legacy():
            return list(DriverWeeklySchedule.objects.filter(driver=d)
                        .order_by("day_of_week").values_list("day_of_week", *_LEGACY_ROW_FIELDS))

        before = legacy()
        days = self.week(mon=("morning", None, None, {"alt": "evening"}),
                         thu=("morning", time(4, 10), time(15, 30),
                              {"day_earliest": time(4), "day_latest": time(15, 30)}),
                         sat=("evening", time(14, 15), time(2, 15),
                              {"day_latest": time(2, 15), "day_latest_next_day": True}))
        rs.save_regular_shift(d, days, self.manager, role_template_id=self.t["morning"].id)
        after = legacy()
        self.assertEqual([r for r in after if r[0] == 3], before)  # the legacy row is untouched
        thu = DriverWeeklySchedule.objects.get(driver=d, day_of_week=3)
        self.assertEqual((thu.shift_template_id, thu.shift_start, thu.shift_end,
                          thu.alt_template_id, thu.day_earliest_start, thu.day_latest_finish,
                          thu.day_latest_finish_next_day),
                         (self.t["morning"].id, time(4, 10), time(15, 30), None, time(4),
                          time(15, 30), False))
        mon = DriverWeeklySchedule.objects.get(driver=d, day_of_week=0)
        self.assertEqual((mon.alt_template_id, mon.shift_start, mon.shift_end),
                         (self.t["evening"].id, None, None))
        self.assertEqual((mon.is_available, mon.start_hour, mon.end_hour, mon.flexible),
                         (True, 5, 19, False))                 # a new row copies the defaults
        self.assertTrue(DriverWeeklySchedule.objects.get(driver=d, day_of_week=5)
                        .day_latest_finish_next_day)
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk)), days)

        # Setting a day Off clears its options too, and still leaves the legacy fields be.
        rs.save_regular_shift(d, self.week(), self.manager)
        self.assertEqual(legacy(), after)
        for row in DriverWeeklySchedule.objects.filter(driver=d):
            self.assertEqual((row.shift_template_id, row.alt_template_id, row.shift_start,
                              row.shift_end, row.day_earliest_start, row.day_latest_finish,
                              row.day_latest_finish_next_day),
                             (None, None, None, None, None, None, False))
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk)), self.week())

    def test_save_drops_next_day_without_a_finish_by(self):
        # "Next day" alone means nothing; an editor must not read it back ticked.
        d = _driver()
        rs.save_regular_shift(d, self.week(mon=("evening", None, None,
                                                {"day_latest_next_day": True})), self.manager)
        row = DriverWeeklySchedule.objects.get(driver=d, day_of_week=0)
        self.assertEqual((row.day_latest_finish, row.day_latest_finish_next_day), (None, False))


class RegularWindowTests(_Fixture):
    def window(self, day, **hard):
        return rs.regular_window(day, rs.templates_by_id(), **{**_NO_HARD, **hard})

    def test_regular_window_morning_evening_midday(self):
        W = rs.RegularWindow
        days = self.week(mon=("morning", time(4, 10), time(15, 30)),
                         tue=("evening", time(14, 15), time(2, 15)),
                         wed=("midday", time(7), time(19)),
                         thu=("evening", time(6), time(11)))
        self.assertEqual(self.window(days[0]), W(250, 970, "morning", 720))
        self.assertEqual(self.window(days[1]), W(855, 1575, "evening", 720))
        self.assertEqual(self.window(days[2]), W(420, 1140, "midday", 720))
        self.assertEqual(self.window(days[3]), W(0, 660, "evening", 720))   # never before 00:00
        self.assertIsNone(self.window(days[4]))                             # Off

    def test_regular_window_float(self):
        W = rs.RegularWindow
        day = self.week(mon=("float", None, None))[0]
        self.assertEqual(self.window(day), W(180, 1575, "float", 720))
        typed = self.week(mon=("float", time(9), time(17)))[0]
        self.assertEqual(self.window(typed), W(180, 1575, "float", 720))    # labels only

    def test_regular_window_morning_or_evening(self):
        W = rs.RegularWindow
        day = self.week(mon=("morning", time(4, 10), time(15, 30), {"alt": "evening"}))[0]
        self.assertEqual(self.window(day), W(180, 1575, "float", 720))
        short = copy.copy(self.t["evening"])
        short.max_span_minutes = 600
        templates = {**rs.templates_by_id(), short.id: short}
        self.assertEqual(rs.regular_window(day, templates, **_NO_HARD),
                         W(180, 1575, "float", 600))                        # the smaller span
        midday_or_evening = self.week(mon=("midday", None, None, {"alt": "evening"}))[0]
        self.assertEqual(self.window(midday_or_evening), W(360, 1575, "float", 720))

    def test_regular_window_clipped_by_day_and_hard_limits(self):
        W = rs.RegularWindow
        days = self.week(mon=("float", None, None),
                         tue=("float", None, None, {"day_earliest": time(6)}),
                         thu=("morning", time(4, 10), time(15, 30), {"day_latest": time(15)}),
                         sat=("evening", time(14, 15), time(2, 15),
                              {"day_latest": time(1), "day_latest_next_day": True}))
        self.assertEqual(self.window(days[3], hard_lo=270), W(270, 900, "morning", 720))
        self.assertEqual(self.window(days[5]), W(855, 1500, "evening", 720))
        self.assertEqual(self.window(days[0], hard_lo=300, hard_hi=1500),
                         W(300, 1500, "float", 720))
        self.assertEqual(self.window(days[1]), W(360, 1575, "float", 720))
        self.assertEqual(self.window(days[1], hard_lo=420), W(420, 1575, "float", 720))
        self.assertIsNone(self.window(days[0], hard_hi=120))               # nothing left

    def test_regular_window_end_band_after_midnight(self):
        # A shape edited (Task 8) to end wholly after midnight still lines its
        # latest end up after its start, so open days compare ends on one day.
        W = rs.RegularWindow
        late_evening = copy.copy(self.t["evening"])
        late_evening.end_earliest, late_evening.end_latest = time(0, 30), time(2, 15)
        self.assertEqual(late_evening.end_band_minutes(), (30, 135))
        templates = {**rs.templates_by_id(), late_evening.id: late_evening}
        either = self.week(mon=("morning", None, None, {"alt": "evening"}))[0]
        self.assertEqual(rs.regular_window(either, templates, **_NO_HARD),
                         W(180, 1575, "float", 720))
        late_float = copy.copy(self.t["float"])
        late_float.end_earliest, late_float.end_latest = time(0, 30), time(2, 15)
        templates = {**rs.templates_by_id(), late_float.id: late_float}
        self.assertEqual(rs.regular_window(self.week(mon=("float", None, None))[0], templates,
                                           **_NO_HARD),
                         W(180, 1575, "float", 720))

    def test_regular_window_needs_a_known_shape_and_both_times(self):
        self.assertIsNone(self.window(DayShift(0, 99999, None, None)))
        self.assertIsNone(self.window(self.week(mon=("morning", time(4, 10), None))[0]))
        tpl = rs.templates_by_id()
        self.assertIsNone(rs.effective_minutes(DayShift(0, 99999, time(4, 10), time(15, 30)), tpl))
        self.assertIsNone(rs.effective_minutes(DayShift(0, 99999, None, None), tpl))


class DayOptionLabelTests(_Fixture):
    def test_day_label_variants(self):
        tpl = rs.templates_by_id()
        cases = [
            (("morning", time(4, 10), time(15, 30)), "Morning 4:10 AM – 3:30 PM"),
            (("morning", None, None), "Morning (usual times)"),
            (("morning", None, None, {"alt": "evening"}), "Morning or Evening"),
            (("morning", time(4, 10), time(15, 30), {"alt": "evening"}), "Morning or Evening"),
            (("float", None, None), "Float"),
            (("float", time(9), time(17)), "Float"),
            (("morning", time(4, 10), time(15, 30), {"day_latest": time(15)}),
             "Morning 4:10 AM – 3:30 PM · done by 3 PM"),
            (("midday", None, None, {"day_earliest": time(6)}),
             "Midday (usual times) · not before 6 AM"),
            (("evening", time(14, 15), time(2, 15),
              {"day_earliest": time(15), "day_latest": time(1), "day_latest_next_day": True}),
             "Evening 2:15 PM – 2:15 AM · not before 3 PM · done by 1 AM (next day)"),
        ]
        for spec, label in cases:
            with self.subTest(label=label):
                self.assertEqual(rs.day_label(self.week(mon=spec)[0], tpl), label)
        self.assertEqual(rs.day_label(self.week()[0], tpl), "Off")

    def test_summary_label_with_day_options(self):
        tpl = rs.templates_by_id()
        morning = ("morning", time(4, 10), time(15, 30))
        days = self.week(mon=morning, tue=morning, wed=morning,
                         thu=(*morning, {"day_latest": time(15)}),
                         fri=("float", None, None),
                         sat=("morning", None, None, {"alt": "evening"}),
                         sun=("midday", None, None))
        self.assertEqual(rs.summary_label(days, tpl),
                         "Mon–Wed Morning 4:10 AM–3:30 PM · Thu Morning 4:10 AM–3:30 PM, "
                         "done by 3 PM · Fri Float · Sat Morning or Evening · "
                         "Sun Midday (usual times)")
        floats = self.week(mon=("float", time(4), time(15)), tue=("float", None, None))
        self.assertEqual(rs.summary_label(floats, tpl), "Mon–Tue Float")


# ════════════════════════════════════════════════════════════════════════════
# Task 7: Drivers -> Regular Shifts, the editor and the switch
# ════════════════════════════════════════════════════════════════════════════

_EDITOR_FIELDS = ("template", "alt_template", "start", "end", "day_earliest_start",
                  "day_latest_finish")


@override_settings(GOOGLE_MAPS_API_KEY="")
class RegularShiftPageTests(_Fixture):
    """The list every staff user sees, the editor only a manager saves from,
    and the switch only a manager flips."""

    def setUp(self):
        super().setUp()
        # The pages read "today" through one helper; pin it to the fixture's Monday.
        today = mock.patch("drivers.regular_shift_views._today", return_value=TODAY)
        today.start()
        self.addCleanup(today.stop)
        # Day Setup leaves driver id 6 out by id; a test driver may get that id.
        no_ids = mock.patch.object(day_setup, "DAY_SETUP_EXCLUDE_DRIVER_IDS", set())
        no_ids.start()
        self.addCleanup(no_ids.stop)

    # ── helpers ──
    def list_page(self, user=None):
        self.client.force_login(user or self.dispatcher)
        return self.client.get(reverse("regular_shifts"))

    @staticmethod
    def edit_url(driver):
        return reverse("regular_shift_edit", args=[driver.id])

    def payload(self, role, works_on=(), **days):
        """The editor's POST, as the browser sends it: the usual shift (a kind),
        the working days (0-6), and per day key (mon..sun) that day's overrides —
        template / alt_template as a kind, times as "HH:MM", and
        day_latest_finish_next_day=True."""
        data = {"role": str(self.t[role].id) if role else "",
                "works_on": [str(i) for i in works_on],
                "days-TOTAL_FORMS": "7", "days-INITIAL_FORMS": "7",
                "days-MIN_NUM_FORMS": "0", "days-MAX_NUM_FORMS": "7"}
        for i, key in enumerate(_DAY_KEYS):
            spec = dict(days.get(key, {}))
            for name in ("template", "alt_template"):
                if spec.get(name):
                    spec[name] = str(self.t[spec[name]].id)
            for name in _EDITOR_FIELDS:
                data[f"days-{i}-{name}"] = spec.get(name, "")
            if spec.get("day_latest_finish_next_day"):
                data[f"days-{i}-day_latest_finish_next_day"] = "on"
        return data

    @staticmethod
    def messages_of(resp):
        return [(m.level_tag, str(m)) for m in resp.context["messages"]]

    @staticmethod
    def switch_button(resp):
        found = re.search(r'<button[^>]*id="switch-button"[^>]*>.*?</button>',
                          resp.content.decode(), re.S)
        return found.group(0) if found else None

    @staticmethod
    def card(html):
        """The profile's Shift facts card: it sits straight above Weekly Schedule."""
        start = html.index('id="shift-facts"')
        return html[start:html.index("Weekly Schedule", start)]

    def confirm(self, driver, days=None, role="morning"):
        rs.save_regular_shift(driver, days if days is not None else self.week(), self.manager,
                              role_template_id=self.t[role].id if role else None)

    # ── the list ──
    def test_list_splits_needs_and_confirmed(self):
        amy, bob = _driver("Amy", "Alpha"), _driver("Bob", "Bravo")
        _driver("Aff", "Iliate", driver_type="affiliate")         # never on the list
        morning = ("morning", time(4, 10), time(15, 30))
        self.confirm(amy, self.week(**{k: morning for k in _DAY_KEYS[:5]}))
        Driver.objects.filter(pk=amy.pk).update(
            regular_shift_confirmed_at=timezone.make_aware(datetime(2026, 10, 4, 14, 0)))
        resp = self.list_page()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["driver"] for r in resp.context["needs"]], [bob])
        self.assertEqual([r["driver"] for r in resp.context["confirmed"]], [amy])
        row = resp.context["confirmed"][0]
        self.assertEqual((row["summary"], row["role"], row["confirmed_by"]),
                         ("Mon–Fri Morning 4:10 AM–3:30 PM", "Morning driver", "rs_manager"))
        html = resp.content.decode()
        for text in ("Regular Shifts", "Needs a regular shift (1)", "Confirmed (1)",
                     "Bob Bravo", "Amy Alpha", "Morning driver",
                     "Mon–Fri Morning 4:10 AM–3:30 PM", "confirmed by rs_manager on Oct 4"):
            self.assertIn(text, html)
        self.assertNotIn("Iliate", html)
        # A dispatcher can open a confirmed week to read it, but has nothing to
        # confirm and no switch.
        self.assertIn(self.edit_url(amy), html)
        self.assertNotIn(self.edit_url(bob), html)
        self.assertNotIn(reverse("regular_shift_switch"), html)
        self.assertIsNone(self.switch_button(resp))
        # A manager confirms Bob from here, and edits Amy.
        html = self.list_page(self.manager).content.decode()
        self.assertIn(self.edit_url(bob), html)
        self.assertIn("Review &amp; confirm", html)
        self.assertIn(self.edit_url(amy), html)

    def test_list_shows_no_recent_trips(self):
        # Review Focus 4: nothing in 8 weeks says so, and the week still confirms.
        quiet, busy = _driver("Nev", "Quiet"), _driver("Bea", "Busy")
        for day in _lookback(0)[:5]:
            self.leg(busy, day, 5, 0, MCO, DISNEY)
            self.leg(busy, day, 9, 0, DISNEY, MCO)
        resp = self.list_page()
        rows = {r["driver"].pk: r for r in resp.context["needs"]}
        self.assertEqual(rows[quiet.pk]["summary"], "no trips in the last 8 weeks")
        self.assertEqual(rows[busy.pk]["summary"], "Mon Morning 4:35 AM–9:50 AM")
        self.assertEqual(rows[busy.pk]["role"], "Morning driver")    # what the weeks point to
        self.assertEqual(rows[quiet.pk]["role"], "")
        self.assertContains(resp, "no trips in the last 8 weeks")
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(self.edit_url(quiet)), "No trips in the last 8 weeks")
        resp = self.client.post(self.edit_url(quiet), self.payload("float"))
        self.assertRedirects(resp, reverse("regular_shifts"))
        quiet = Driver.objects.get(pk=quiet.pk)
        self.assertTrue(quiet.has_regular_shift)
        self.assertEqual(rs.current_days(quiet), self.week())         # Off every day (S2)

    # ── the editor ──
    def test_editor_prefills_from_suggestion(self):
        d = _driver()
        for day in _lookback(0)[:5] + _lookback(1)[:6]:
            self.leg(d, day, 5, 0, MCO, DISNEY)
            self.leg(d, day, 9, 0, DISNEY, MCO)
        self.client.force_login(self.manager)
        resp = self.client.get(self.edit_url(d))
        self.assertEqual(resp.status_code, 200)
        shift_form, formset = resp.context["shift_form"], resp.context["day_formset"]
        self.assertEqual(shift_form.initial["role"], self.t["morning"].id)
        self.assertEqual(shift_form.initial["works_on"], [0, 1])
        self.assertEqual(len(formset.forms), 7)
        monday = formset.forms[0].initial
        # "Same as usual" (the usual shift is Morning) with the suggested times.
        self.assertEqual((monday["template"], monday["start"], monday["end"]),
                         (None, time(4, 35), time(9, 50)))
        rows = resp.context["rows"]
        self.assertEqual(rows[0]["evidence"],
                         "Worked 5 of the last 8 Mondays · usual 4:35 AM – 9:50 AM")
        self.assertEqual(rows[1]["evidence"],
                         "Worked 6 of the last 8 Tuesdays · usual 4:35 AM – 9:50 AM")
        self.assertEqual(rows[2]["evidence"], "Not a regular day")
        self.assertEqual((rows[0]["label"], rows[2]["label"]),
                         ("Morning 4:35 AM – 9:50 AM", "Off"))
        html = resp.content.decode()
        for text in ('value="04:35"', "Suggested from the last 8 weeks", "<details",
                     'id="confirm-shift"', "Usual shift", "Works on", "Any day different?"):
            self.assertIn(text, html)

    def test_editor_prefills_from_confirmed_shift(self):
        d = _driver()
        days = self.week(mon=("morning", time(4, 10), time(15, 30)),
                         tue=("evening", time(14, 15), time(2, 15),
                              {"day_latest": time(2, 30), "day_latest_next_day": True}),
                         wed=("morning", None, None, {"alt": "evening"}))
        self.confirm(d, days)
        self.client.force_login(self.manager)
        resp = self.client.get(self.edit_url(d))
        shift_form, forms = resp.context["shift_form"], resp.context["day_formset"].forms
        self.assertEqual(shift_form.initial["role"], self.t["morning"].id)
        self.assertEqual(shift_form.initial["works_on"], [0, 1, 2])
        self.assertIsNone(forms[0].initial["template"])
        self.assertEqual((forms[1].initial["template"], forms[1].initial["day_latest_finish"],
                          forms[1].initial["day_latest_finish_next_day"]),
                         (self.t["evening"].id, time(2, 30), True))
        self.assertEqual(forms[2].initial["alt_template"], self.t["evening"].id)
        self.assertContains(resp, "Confirmed by rs_manager on")
        # Sending the page straight back keeps the week exactly as it was.
        resp = self.client.post(self.edit_url(d), self.payload(
            "morning", works_on=[0, 1, 2], mon={"start": "04:10", "end": "15:30"},
            tue={"template": "evening", "start": "14:15", "end": "02:15",
                 "day_latest_finish": "02:30", "day_latest_finish_next_day": True},
            wed={"alt_template": "evening"}))
        self.assertRedirects(resp, reverse("regular_shifts"))
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk)), days)

    def test_manager_confirms(self):
        d = _driver()
        self.client.force_login(self.manager)
        morning = {"start": "04:10", "end": "15:30"}
        resp = self.client.post(self.edit_url(d), self.payload(
            "morning", works_on=range(5), mon={"start": "02:00", "end": "11:00"},
            tue=morning, wed=morning, thu=morning, fri=morning), follow=True)
        self.assertRedirects(resp, reverse("regular_shifts"))
        self.assertEqual(self.messages_of(resp), [
            ("success", "Regular shift confirmed for Sam Driver."),
            ("warning", "Monday: 2 AM–11 AM is outside the usual Morning shape "
                        "(leaves 3 AM–6 AM, back 12 PM–4 PM)."),
        ])
        d = Driver.objects.get(pk=d.pk)
        self.assertTrue(d.has_regular_shift)
        self.assertEqual(d.regular_shift_confirmed_by, self.manager)
        self.assertEqual(d.shift_role_id, self.t["morning"].id)
        self.assertEqual(rs.current_days(d), self.week(
            mon=("morning", time(2), time(11)),
            **{k: ("morning", time(4, 10), time(15, 30)) for k in _DAY_KEYS[1:5]}))
        self.assertEqual([r["driver"] for r in resp.context["confirmed"]], [d])

    def test_editor_rejects_13h_day(self):
        d = _driver()
        self.client.force_login(self.manager)
        resp = self.client.post(self.edit_url(d), self.payload(
            "morning", works_on=[0], mon={"start": "04:00", "end": "17:00"}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["day_formset"].non_form_errors(),
                         ["Monday: a shift longer than 12 hours isn't allowed."])
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=d).exists())
        d.refresh_from_db()
        self.assertFalse(d.has_regular_shift)
        self.assertIsNone(d.shift_role_id)

    def test_editor_needs_a_usual_shift(self):
        d = _driver()
        self.client.force_login(self.manager)
        resp = self.client.post(self.edit_url(d), self.payload(None, works_on=[0]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("role", resp.context["shift_form"].errors)
        d.refresh_from_db()
        self.assertFalse(d.has_regular_shift)

    def test_editor_blank_times_mean_usual(self):
        d = _driver()
        self.client.force_login(self.manager)
        resp = self.client.post(self.edit_url(d), self.payload("morning", works_on=[0, 2]))
        self.assertRedirects(resp, reverse("regular_shifts"))
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk)),
                         self.week(mon=("morning", None, None), wed=("morning", None, None)))
        # The editor says what blank means: the shape's usual times.
        page = self.client.get(self.edit_url(d))
        row = page.context["rows"][0]
        self.assertEqual((row["label"], row["usual"]), ("Morning (usual times)", "6 AM – 4 PM"))
        self.assertIn('placeholder="06:00"', str(page.context["day_formset"].forms[0]["start"]))
        self.client.force_login(self.dispatcher)
        profile = self.client.get(reverse("driver_profile", args=[d.id]))
        self.assertEqual(profile.context["regular_rows"][0], ("Monday", "Morning (usual times)"))

    def test_editor_role_and_days(self):
        # A Float driver on Mon/Tue/Wed: every other day is Off, whatever its row says.
        d = _driver()
        self.client.force_login(self.manager)
        resp = self.client.post(self.edit_url(d), self.payload(
            "float", works_on=[0, 1, 2],
            thu={"template": "evening", "start": "14:15", "end": "02:15"}))
        self.assertRedirects(resp, reverse("regular_shifts"))
        d = Driver.objects.get(pk=d.pk)
        self.assertEqual(d.shift_role_id, self.t["float"].id)
        floats = ("float", None, None)
        self.assertEqual(rs.current_days(d), self.week(mon=floats, tue=floats, wed=floats))
        self.assertFalse(DriverWeeklySchedule.objects.filter(
            driver=d, day_of_week__gte=3, shift_template__isnull=False).exists())

    def test_editor_thursday_done_by_3pm(self):
        d = _driver()
        self.client.force_login(self.manager)
        times = {"start": "04:10", "end": "14:30"}
        resp = self.client.post(self.edit_url(d), self.payload(
            "morning", works_on=range(5), mon=times, tue=times, wed=times,
            thu={**times, "day_latest_finish": "15:00"}, fri=times))
        self.assertRedirects(resp, reverse("regular_shifts"))
        m = self.t["morning"].id
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk))[3],
                         DayShift(3, m, time(4, 10), time(14, 30), day_latest=time(15)))
        self.client.force_login(self.dispatcher)
        profile = self.client.get(reverse("driver_profile", args=[d.id]))
        self.assertEqual(profile.context["regular_rows"][3],
                         ("Thursday", "Morning 4:10 AM – 2:30 PM · done by 3 PM"))
        self.assertContains(profile, "· done by 3 PM")
        # A typed end after that day's own finish-by is refused, naming the day.
        self.client.force_login(self.manager)
        resp = self.client.post(self.edit_url(d), self.payload(
            "morning", works_on=[3], thu={"start": "04:10", "end": "15:30",
                                          "day_latest_finish": "15:00"}))
        self.assertEqual(resp.context["day_formset"].non_form_errors(),
                         ["Thursday: ends at 3:30 PM — after that day's finish-by (3 PM)."])

    def test_editor_morning_or_evening_day(self):
        d = _driver()
        self.client.force_login(self.manager)
        resp = self.client.post(self.edit_url(d), self.payload(
            "morning", works_on=[5], sat={"alt_template": "evening"}))
        self.assertRedirects(resp, reverse("regular_shifts"))
        self.assertEqual(rs.current_days(Driver.objects.get(pk=d.pk))[5],
                         DayShift(5, self.t["morning"].id, None, None,
                                  alt_template_id=self.t["evening"].id))
        row = self.list_page().context["confirmed"][0]
        self.assertEqual(row["summary"], "Sat Morning or Evening")
        # "Or also" the same shape as the day's own is refused.
        self.client.force_login(self.manager)
        resp = self.client.post(self.edit_url(d), self.payload(
            "morning", works_on=[5], sat={"alt_template": "morning"}))
        self.assertEqual(resp.context["day_formset"].non_form_errors(),
                         ["Saturday: the second shift must be different from the first."])

    def test_dispatcher_cannot_post_editor(self):
        d = _driver()
        self.client.force_login(self.dispatcher)
        page = self.client.get(self.edit_url(d))
        self.assertEqual(page.status_code, 200)                     # staff may read it
        self.assertNotContains(page, 'id="confirm-shift"')
        self.assertContains(page, "<fieldset disabled")
        resp = self.client.post(self.edit_url(d), self.payload("morning", works_on=[0]))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=d).exists())
        d.refresh_from_db()
        self.assertFalse(d.has_regular_shift)

    def test_editor_only_for_staff_and_in_house_chauffeurs(self):
        d = _driver()
        self.client.force_login(User.objects.create_user("rs_guest", password="x"))
        self.assertEqual(self.client.get(self.edit_url(d)).status_code, 302)
        self.assertEqual(self.client.get(reverse("regular_shifts")).status_code, 302)
        self.client.force_login(self.manager)
        for other in (_driver("Aff", "Iliate", driver_type="affiliate"),
                      _driver("Op", "Erator", portal_role="operator")):
            with self.subTest(driver=str(other)):
                self.assertEqual(self.client.get(self.edit_url(other)).status_code, 404)

    # ── the switch ──
    def test_switch_button_disabled_until_empty(self):
        amy = _driver("Amy", "Alpha")
        resp = self.list_page(self.manager)
        self.assertFalse(resp.context["switch_on"])
        button = self.switch_button(resp)
        self.assertIn("Use regular shifts for auto-assign", button)
        self.assertIn("disabled", button)
        self.assertContains(resp, "1 driver still needs a regular shift")
        self.assertContains(resp, "today's hours")
        self.confirm(amy)
        resp = self.list_page(self.manager)
        button = self.switch_button(resp)
        self.assertIn("Use regular shifts for auto-assign", button)
        self.assertNotIn("disabled", button)
        self.assertNotContains(resp, "still need")

    def test_switch_post_refused_with_message(self):
        _driver("Abe", "Adams")
        self.client.force_login(self.manager)
        resp = self.client.post(reverse("regular_shift_switch"), {"on": "1"}, follow=True)
        self.assertRedirects(resp, reverse("regular_shifts"))
        self.assertEqual(self.messages_of(resp),
                         [("error", "1 driver still needs a regular shift: Abe Adams")])
        self.assertFalse(SchedulerSettings.objects.filter(regular_shift_windows=True).exists())
        self.assertFalse(rs.regular_windows_on())

    def test_switch_post_turns_on(self):
        self.confirm(_driver("Amy", "Alpha"))
        self.client.force_login(self.manager)
        resp = self.client.post(reverse("regular_shift_switch"), {"on": "1"}, follow=True)
        self.assertRedirects(resp, reverse("regular_shifts"))
        self.assertEqual(self.messages_of(resp),
                         [("success", "Auto-assign now uses regular shifts.")])
        self.assertTrue(SchedulerSettings.objects.get(pk=1).regular_shift_windows)
        self.assertTrue(rs.regular_windows_on())
        self.assertTrue(resp.context["switch_on"])
        self.assertIn("Go back to today's hours", self.switch_button(resp))
        resp = self.client.post(reverse("regular_shift_switch"), {"on": "0"}, follow=True)
        self.assertEqual(self.messages_of(resp),
                         [("success", "Auto-assign is back on today's hours.")])
        self.assertFalse(SchedulerSettings.objects.get(pk=1).regular_shift_windows)
        self.assertFalse(rs.regular_windows_on())

    def test_switch_post_dispatcher_forbidden(self):
        self.confirm(_driver("Amy", "Alpha"))
        self.client.force_login(self.dispatcher)
        resp = self.client.post(reverse("regular_shift_switch"), {"on": "1"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(rs.regular_windows_on())
        self.assertFalse(SchedulerSettings.objects.filter(regular_shift_windows=True).exists())
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("regular_shift_switch")).status_code, 405)

    def test_new_driver_after_switch_on_listed_switch_stays_on(self):
        # Review Focus 3: someone added or reactivated after the switch is on is
        # listed, the switch stays on, and his hours are exactly today's.
        amy = _driver("Amy", "Alpha")
        self.confirm(amy, self.week(mon=("morning", time(4, 10), time(15, 30))))
        self.assertEqual(rs.set_regular_windows(True, self.manager), (True, ""))
        bob = _driver("Bob", "Bravo")
        cal = _driver("Cal", "Clark", is_active=False)
        Driver.objects.filter(pk=cal.pk).update(is_active=True)
        resp = self.list_page(self.manager)
        self.assertEqual([r["driver"] for r in resp.context["needs"]], [bob, cal])
        self.assertTrue(resp.context["switch_on"])
        self.assertContains(resp, "2 drivers still need a regular shift")
        button = self.switch_button(resp)
        self.assertIn("Go back to today's hours", button)
        self.assertNotIn("disabled", button)
        self.assertTrue(rs.regular_windows_on())
        self.assertTrue(SchedulerSettings.objects.get(pk=1).regular_shift_windows)
        for driver in (bob, cal):
            fresh = Driver.objects.get(pk=driver.pk)
            for i in range(7):
                day = TODAY + timedelta(days=i)
                eff = resolve_effective_availability(fresh, day)
                self.assertEqual(eff, resolve_effective_availability(fresh, day,
                                                                     regular_windows=False))
                self.assertIsNone(eff["window_start_min"])

    # ── the way in: the navbar and the profile card ──
    def test_navbar_link(self):
        link = (r'class="dropdown-item ?(active)?"\s+href="%s"><i class="bi bi-calendar2-check me-2">'
                r'</i>Regular Shifts</a>' % re.escape(reverse("regular_shifts")))
        toggle = r'dropdown-toggle text-white active"[^>]*>\s*<i class="bi bi-people-fill me-1">'
        self.client.force_login(self.dispatcher)
        html = self.client.get(reverse("drivers_extend")).content.decode()
        found = re.search(link, html)
        self.assertIsNotNone(found)                              # every staff user has it
        self.assertIsNone(found.group(1))
        self.assertLess(html.index("Edit Schedules"), found.start())   # right after Edit Schedules
        d = _driver()
        for url in (reverse("regular_shifts"), self.edit_url(d)):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertEqual(re.search(link, html).group(1), "active")
                self.assertRegex(html, toggle)

    def test_profile_card_links_to_editor_for_manager(self):
        d = _driver()
        profile = reverse("driver_profile", args=[d.id])
        self.client.force_login(self.manager)
        card = self.card(self.client.get(profile).content.decode())
        self.assertIn(f'href="{self.edit_url(d)}"', card)
        self.assertIn("Set regular shift", card)
        self.confirm(d, self.week(mon=("morning", time(4, 10), time(15, 30))))
        card = self.card(self.client.get(profile).content.decode())
        self.assertIn(f'href="{self.edit_url(d)}"', card)
        self.assertIn("Edit regular shift", card)
        self.assertNotIn("Set regular shift", card)
        # In edit mode too.
        card = self.card(self.client.get(profile, {"edit": "1"}).content.decode())
        self.assertIn("Edit regular shift", card)
        # A dispatcher reads the card; an affiliate never gets a regular shift.
        self.client.force_login(self.dispatcher)
        self.assertNotIn(self.edit_url(d), self.card(self.client.get(profile).content.decode()))
        aff = _driver("Aff", "Iliate", driver_type="affiliate")
        self.client.force_login(self.manager)
        card = self.card(self.client.get(reverse("driver_profile", args=[aff.id])).content.decode())
        self.assertNotIn(self.edit_url(aff), card)
        self.assertNotIn("regular shift</a>", card)
