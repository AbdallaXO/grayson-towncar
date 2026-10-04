"""The regular-shift module (structured shifts, Stage 1): suggestions from the
last 8 weeks, validation, saving, labels, the roster and the switch.

Two review-focus guards live here: confirming a regular shift must not move
anything the legacy availability resolver returns (Review Focus 2), and saving
either legacy schedule editor must leave a confirmed regular shift alone
(Review Focus 1).

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_regular_shifts
"""
import copy
import json
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

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
        """Seven DayShifts; a day given as (kind, start, end) works, the rest are Off."""
        out = []
        for i, key in enumerate(_DAY_KEYS):
            spec = days.get(key)
            if spec is None:
                out.append(DayShift(i, None, None, None))
            else:
                kind, start, end = spec
                out.append(DayShift(i, self.t[kind].id, start, end))
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
        # Last clear 09:00 + 34.8 (P50 departure tail) + 27 (MCO -> base + fuel)
        # = 10:01.8, rounded up to 10:05.
        self.assertEqual(monday.end, time(10, 5))

    def test_suggest_irregular_weekday_is_off(self):
        d = _driver()
        for day in _lookback(1)[:3]:
            self.leg(d, day, 5, 0)
        tuesday = rs.suggest_regular_shifts([d], TODAY)[d.id][1]
        self.assertEqual(tuesday.weeks_worked, 3)
        self.assertIsNone(tuesday.template_id)
        self.assertIsNone(tuesday.start)
        self.assertIsNone(tuesday.end)

    def test_suggest_evening_start_includes_report_and_end_includes_night_return(self):
        d = _driver()
        firsts = [(14, 50), (15, 0), (15, 0), (15, 10), (15, 20)]
        for day, (hh, mm) in zip(_lookback(2), firsts):
            self.leg(d, day, hh, mm, MCO, DISNEY)
            self.leg(d, day, 23, 0, MCO, MCO)
        wednesday = rs.suggest_regular_shifts([d], TODAY)[d.id][2]
        self.assertEqual(wednesday.template_id, self.t["evening"].id)
        # Raw starts 14:28..14:58 (pickup - 22) sit in the Evening band; minus the
        # 25-min evening report -> median 14:13, rounded down to 14:10.
        self.assertEqual(wednesday.start, time(14, 10))
        # 23:00 + 75.5 (P50 arrival tail) + 61 (wash, fuel, base) = 01:16.5 -> 01:20.
        self.assertEqual(wednesday.end, time(1, 20))

    def test_suggest_night_tail_counts_for_previous_day(self):
        d = _driver()
        for day in _lookback(3)[:5]:                       # Thursdays
            self.leg(d, day, 16, 0, MCO, DISNEY)
            self.leg(d, day + timedelta(days=1), 0, 30, MCO, MCO)   # Friday 00:30
        days = rs.suggest_regular_shifts([d], TODAY)[d.id]
        thursday, friday = days[3], days[4]
        self.assertEqual(thursday.template_id, self.t["evening"].id)
        self.assertEqual(thursday.start, time(15, 10))     # 16:00 - 22 - 25 = 15:13 -> 15:10
        # 00:30 next day + 75.5 + 61 = 02:46.5 -> 02:50, inside 15:10 + 12h.
        self.assertEqual(thursday.end, time(2, 50))
        self.assertEqual(friday.weeks_worked, 0)
        self.assertIsNone(friday.template_id)

    def test_suggest_end_uses_each_legs_own_return(self):
        # The leg that clears last isn't always the last one back at base: the
        # Port leg clears ~22 min earlier but has a far longer drive home.
        d = _driver()
        for day in _lookback(2)[:5]:                       # Wednesdays
            self.leg(d, day, 15, 0, MCO, DISNEY)
            self.leg(d, day, 20, 0, MCO, MCO)              # clears 21:15.5, back 22:16.5
            self.leg(d, day, 20, 0, PORT, PORT)            # clears 20:53.6, back 22:49.6
        wednesday = rs.suggest_regular_shifts([d], TODAY)[d.id][2]
        self.assertEqual(wednesday.template_id, self.t["evening"].id)
        self.assertEqual(wednesday.start, time(14, 10))
        # 20:00 + 53.6 (P50 other tail) + 116 (Port -> MCO, wash, fuel, base) -> 22:50.
        self.assertEqual(wednesday.end, time(22, 50))

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
        # 05:30 + 53.6 (P50 other tail) + 50 (Disney -> base + fuel) = 07:13.6 -> 07:15.
        self.assertEqual(monday.end, time(7, 15))

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
        # 01:30 - 22 = 01:08 -> 01:05; 01:30 + 75.5 + 27 = 03:12.5 -> 03:15.
        self.assertEqual((friday.start, friday.end), (time(1, 5), time(3, 15)))
        self.assertEqual(DayShift(4, friday.template_id, friday.start, friday.end).minutes(),
                         (65, 195))

    def test_suggest_start_never_before_midnight(self):
        # A 00:10 pickup with nothing the evening before would leave base at
        # 23:48 the day before; the weekday's start is held at 00:00 instead.
        d = _driver()
        for day in _lookback(6)[:4]:                       # Sundays; Saturdays idle
            self.leg(d, day, 0, 10, MCO, MCO)
        sunday = rs.suggest_regular_shifts([d], TODAY)[d.id][6]
        self.assertEqual(sunday.template_id, self.t["morning"].id)
        # 00:10 + 75.5 + 27 = 01:52.5 -> 01:55.
        self.assertEqual((sunday.start, sunday.end), (time(0, 0), time(1, 55)))
        self.assertEqual(DayShift(6, sunday.template_id, sunday.start, sunday.end).minutes(),
                         (0, 115))

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
        self.assertEqual(sunday.start, time(15, 10))       # 16:00 - 22 - 25 = 15:13 -> 15:10
        # Ends 16:00 + 75.5 + 91 = 18:46.5 (no tail) and 01:00 + 75.5 + 61 = 03:16.5
        # (tail); median of two each = 23:01.5 -> 23:05. Without today's tail: 18:50.
        self.assertEqual(sunday.end, time(23, 5))
        self.assertEqual(days[0].weeks_worked, 0)          # the 01:00 pickups aren't Mondays

    def test_suggest_majority_shape(self):
        # 3 Morning Mondays and 2 Midday: Morning, with medians over the Morning
        # days only (over all five they'd be 04:55 and 07:30).
        d = _driver()
        mondays = _lookback(0)
        for day, mm in zip(mondays[:3], (0, 10, 20)):
            self.leg(d, day, 5, mm, MCO, DISNEY)           # raw 04:38, 04:48, 04:58
        for day in mondays[3:5]:
            self.leg(d, day, 8, 0, MCO, DISNEY)            # raw 07:38: Midday
        monday = rs.suggest_regular_shifts([d], TODAY)[d.id][0]
        self.assertEqual((monday.weeks_worked, monday.template_id), (5, self.t["morning"].id))
        self.assertEqual(monday.start, time(4, 45))        # median 04:48 -> 04:45
        # Ends 05:10 + 75.5 + 50 = 07:15.5 (the median) -> 07:20.
        self.assertEqual(monday.end, time(7, 20))

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
        # first shape), with Midday-only medians: 07:38 -> 07:35, 08:00 + 75.5 + 91
        # = 10:46.5 -> 10:50.
        self.assertEqual((tuesday.weeks_worked, tuesday.template_id), (4, self.t["midday"].id))
        self.assertEqual((tuesday.start, tuesday.end), (time(7, 35), time(10, 50)))
        # Wednesday 2-2: median raw 04:45 sits in Morning's band, so Morning:
        # 03:20; 03:42 + 75.5 + 50 = 05:47.5 -> 05:50.
        self.assertEqual((wednesday.weeks_worked, wednesday.template_id),
                         (4, self.t["morning"].id))
        self.assertEqual((wednesday.start, wednesday.end), (time(3, 20), time(5, 50)))

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
        # 16:00 + 75.5 + 50 = 18:05.5 would be 13.5h; capped at 04:35 + 12h.
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
        days = [DayShift(0, 99999, time(4, 10), time(15, 30)), DayShift(1, 99999, None, None)]
        self.assertEqual(self.validate(days), [
            "Monday: pick Morning, Midday or Evening, or set the day to Off.",
            "Tuesday: pick Morning, Midday or Evening, or set the day to Off.",
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
                                 "evening": (time(16), time(2, 15))})
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
                         "Monday: pick Morning, Midday or Evening, or set the day to Off.")
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
        days = self.week(mon=("morning", time(4, 10), time(15, 30)),
                         fri=("evening", time(14, 15), time(2, 15)))
        rs.save_regular_shift(d, days, self.manager)
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
        self.assertEqual({t.kind for t in first.values()}, {"morning", "midday", "evening"})
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
        self.assertEqual(rs.summary_label(suggestion, rs.templates_by_id()),
                         "Mon Morning 4:35 AM–10:05 AM")
