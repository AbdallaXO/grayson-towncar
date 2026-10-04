"""The engine reads a confirmed regular shift to the minute (structured shifts, Stage 1).

Task 3c: check_feasibility holds every regular-shift day to its shape's max_span_min,
measured base -> base (leaving base for the first pickup to back at base after the last
clear). That is what keeps Float and "Morning or Evening" days — whose window runs from
the earliest start to the latest end — at 12 hours.

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
        # back at base 16:01 (11h 23m). A 16:30 Disney departure clearing 17:20 at MCO puts
        # him back at 18:21 — 13h 43m base to base.
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
        self.assertEqual(refused.reason, "Outside driver window: base to base 13h 43m > 12h 0m")
        self.assertTrue(allowed.feasible, allowed.reason)
