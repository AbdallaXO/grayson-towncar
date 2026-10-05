"""The engine reads a confirmed regular shift to the minute (structured shifts, Stage 1).

Task 3c: check_feasibility holds every regular-shift day to its shape's max_span_min,
measured base -> base (leaving base for the first pickup to back at base after the last
clear). That is what keeps Float and "Morning or Evening" days — whose window runs from
the earliest start to the latest end — at 12 hours.

Task 4b: base -> base counts only the drive in Stage 1 (07 §13 K10), and a day already
over 12h takes no further planned leg, even one that fits inside it (S20).

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_regular_shift_engine
"""
from datetime import datetime, time
from unittest import mock

from django.test import TestCase

import dispatching.scheduler as sch
from dispatching.scheduler import check_feasibility
from dispatching.tests_founder_brain import D, _leg, _sched, _slot
from drivers.test_support import RegularShiftCacheMixin


def _at(h, m):
    return datetime.combine(D, time(h, m))


class BaseSpanFeasibilityTests(RegularShiftCacheMixin, TestCase):
    """check_feasibility passes the day's base -> base span, with and without the new leg."""

    FLOAT = {"start": 3, "end": 23, "start_min": 180, "end_min": 1575, "kind": "float",
             "source": "regular", "max_span_min": 720, "max_hours": None, "flexible": False}

    def test_check_feasibility_float_day_held_to_12h(self):
        # Leave base 04:38 for the 05:00 MCO arrival; the 14:00 departure clears 15:00 at MCO,
        # back at base 15:12 (10h 34m). A 16:30 Disney departure clearing 17:20 at MCO puts
        # him back at 17:32 — 12h 54m base to base.
        day = _sched(46, [
            _slot(_leg(1, 5, 0, trip="arrival", pickup_loc="MCO Terminal",
                       dropoff_loc="Disney Resort"), end_dt=_at(6, 15)),
            _slot(_leg(2, 14, 0, trip="departure", pickup_loc="Disney Resort",
                       dropoff_loc="MCO Terminal"), end_dt=_at(15, 0)),
        ])
        late = _leg(3, 16, 30, trip="departure", pickup_loc="Disney Resort",
                    dropoff_loc="MCO Terminal")
        no_cap = {k: v for k, v in self.FLOAT.items() if k != "max_span_min"}
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(17, 20)):
            refused = check_feasibility(day, late, D, driver_window=self.FLOAT)
            allowed = check_feasibility(day, late, D, driver_window=no_cap)
        self.assertFalse(refused.feasible)
        self.assertEqual(refused.reason, "Outside driver window: base to base 12h 54m > 12h 0m")
        self.assertTrue(allowed.feasible, allowed.reason)

    def test_check_feasibility_day_already_over_takes_no_more_planned_work(self):
        # S20: a board built by hand that is already past 12h — leave base 04:38 for the
        # 05:00 MCO arrival; the 17:00 departure clears 17:40 at MCO, back at base 17:52
        # (13h 14m). Neither a leg inside that span (it leaves the span unchanged) nor one
        # that ends later is planned onto it. The hole-fill only gets the "already over"
        # reason if check_feasibility passes the span WITHOUT the new leg; with none it
        # would read as "base to base 13h 14m".
        day = _sched(47, [
            _slot(_leg(1, 5, 0, trip="arrival", pickup_loc="MCO Terminal",
                       dropoff_loc="Disney Resort"), end_dt=_at(6, 15)),
            _slot(_leg(2, 17, 0, trip="departure", pickup_loc="Disney Resort",
                       dropoff_loc="MCO Terminal"), end_dt=_at(17, 40)),
        ])
        hole = _leg(3, 10, 0, trip="other", pickup_loc="Disney Resort",
                    dropoff_loc="Disney Resort")
        later = _leg(4, 19, 0, trip="departure", pickup_loc="Disney Resort",
                     dropoff_loc="MCO Terminal")
        no_cap = {k: v for k, v in self.FLOAT.items() if k != "max_span_min"}
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(10, 40)):
            hole_refused = check_feasibility(day, hole, D, driver_window=self.FLOAT)
            hole_allowed = check_feasibility(day, hole, D, driver_window=no_cap)
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(19, 40)):
            later_refused = check_feasibility(day, later, D, driver_window=self.FLOAT)
            later_allowed = check_feasibility(day, later, D, driver_window=no_cap)
        for refused in (hole_refused, later_refused):
            self.assertFalse(refused.feasible)
            self.assertEqual(refused.reason,
                             "Outside driver window: day already over 12h 0m base to base")
        self.assertTrue(hole_allowed.feasible, hole_allowed.reason)
        self.assertTrue(later_allowed.feasible, later_allowed.reason)
