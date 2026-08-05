"""Stage A of the Recovery Advisor: dispatching/board_validation.py — the promoted
turn-slack / post-move-simulation primitives — and the thin views/pickup_policy
delegates that keep every existing call site behaving identically.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_board_validation

What is pinned here and why:
  * The promotion is BEHAVIOR-PRESERVING: views._gap_turn_slack /
    views._pickup_risk / views._revalidate_swap_feasibility must return exactly
    what their promoted homes return (the advisor's core promise is that it and
    the board can never disagree at a threshold — that dies the day a delegate
    drifts).
  * The recorded-pickup re-anchor survives the promotion (reality beats the
    plan, but only on facts).
  * validate_post_move_board's precise "no new problems" test: a NEW negative
    hard-rejects; a pre-existing negative elsewhere never vetoes an unrelated
    fix; a '' -> 'tight' worsening is recorded as a demotion, not a rejection;
    a car-share partner overlap blocks.
"""
from datetime import datetime, date as dt_date, time as dt_time
from unittest.mock import patch

from django.test import TestCase

from dispatching import board_validation as bv
from dispatching import pickup_policy as pp
from dispatching.scheduler import (
    DriverDaySchedule, ScheduleSlot, _make_sim_slot, preload_timing_cache,
)
from dispatching.tests_swap_guards import fake_leg
from dispatching.views import _gap_turn_slack, _pickup_risk

DAY = dt_date(2026, 5, 1)


def _dt(h, m=0):
    return datetime(2026, 5, 1, h, m)


def _leg(leg_id, hh, mm=0, trip="return",
         pickup_loc="Disney Contemporary", dropoff_loc="Disney Polynesian"):
    return fake_leg(leg_id=leg_id, pickup=dt_time(hh, mm), trip=trip,
                    pickup_loc=pickup_loc, dropoff_loc=dropoff_loc)


def _schedules(assignment):
    """{driver_id: [legs]} -> {driver_id: DriverDaySchedule} via the same sim-slot
    builder the greedy engine and swap search use."""
    return {
        did: DriverDaySchedule(
            driver_id=did, driver_name=f"D{did}", driver_type="inhouse",
            slots=[_make_sim_slot(l, DAY) for l in legs])
        for did, legs in assignment.items()
    }


def _legs_by_id(*legs):
    return {l.id: l for l in legs}


class TurnSlackDelegateTests(TestCase):
    """views._gap_turn_slack must be a pure pass-through to the promoted
    board_validation.turn_slack_minutes — including the recorded-pickup re-anchor."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()  # empty in test DB -> table fallbacks, no per-call DB

    def _pair(self):
        # The 2026-07-31 walkthrough from tests_pickup_policy: 2:00 PM gate,
        # MCO -> Disney (clears 3:15 on the plan), then a 3:30 Disney pickup.
        prev = _make_sim_slot(_leg(101, 14, 0, trip="arrival",
                                   pickup_loc="MCO Terminal B",
                                   dropoff_loc="Disney Contemporary"), DAY)
        nxt = _make_sim_slot(_leg(102, 15, 30,
                                  pickup_loc="Disney Polynesian",
                                  dropoff_loc="Disney Boardwalk"), DAY)
        return prev, nxt

    def test_planning_clock_matches_and_is_tight(self):
        prev, nxt = self._pair()
        promoted = bv.turn_slack_minutes(prev, nxt, DAY)
        self.assertEqual(promoted, _gap_turn_slack(prev, nxt, DAY))
        # 15:30 - (15:15 clear + 12 Disney->Disney + 0 pad) = 3 min.
        self.assertEqual(promoted, 3)
        self.assertEqual(pp.turn_band(promoted), "tight")

    def test_recorded_pickup_reanchors_through_both_names(self):
        prev, nxt = self._pair()
        prev_leg = _leg(101, 14, 0, trip="arrival",
                        pickup_loc="MCO Terminal B",
                        dropoff_loc="Disney Contemporary")
        promoted = bv.turn_slack_minutes(prev, nxt, DAY, prev_leg=prev_leg,
                                         prev_picked_up_dt=_dt(14, 30))
        self.assertEqual(
            promoted,
            _gap_turn_slack(prev, nxt, DAY, prev_leg=prev_leg,
                            prev_picked_up_dt=_dt(14, 30)))
        # Picked up 2:30 -> clears 3:00 (dwell is spent) -> 18 min, chip goes clean.
        self.assertEqual(promoted, 18)
        self.assertEqual(pp.turn_band(promoted), "")

    def test_missing_slots_still_return_none(self):
        self.assertIsNone(bv.turn_slack_minutes(None, None, DAY))
        self.assertIsNone(_gap_turn_slack(None, None, DAY))


class BoardTurnBandsTests(TestCase):
    """The pair sweep: stable (driver, prev_leg, next_leg) keys, banded by
    pickup_policy.turn_band, with the optional recorded-pickup re-anchor."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()

    def _board(self):
        return _schedules({1: [
            _leg(101, 14, 0, trip="arrival", pickup_loc="MCO Terminal B",
                 dropoff_loc="Disney Contemporary"),
            _leg(102, 15, 30, pickup_loc="Disney Polynesian",
                 dropoff_loc="Disney Boardwalk"),
        ]})

    def test_planning_clock_bands(self):
        bands = bv.board_turn_bands(self._board(), DAY)
        self.assertEqual(list(bands), [(1, 101, 102)])
        self.assertEqual(bands[(1, 101, 102)], {"slack": 3, "band": "tight"})

    def test_picked_up_by_leg_reanchors(self):
        bands = bv.board_turn_bands(self._board(), DAY,
                                    picked_up_by_leg={101: _dt(14, 30)})
        self.assertEqual(bands[(1, 101, 102)], {"slack": 18, "band": ""})


class PickupRiskDelegateTests(TestCase):
    """views._pickup_risk === pickup_policy.pickup_risk across the whole
    precedence table (GPS truth when fresh, clock as fallback)."""

    CASES = [
        dict(gps_status='at_risk', gps_eta_mins=30, pickup_overdue=True,
             pickup_stalled=True, overdue_mins=5),
        dict(gps_status='late', gps_eta_mins=None, pickup_overdue=True,
             pickup_stalled=False, overdue_mins=9),
        dict(gps_status='', gps_eta_mins=None, pickup_overdue=True,
             pickup_stalled=True, overdue_mins=7),           # clock critical
        dict(gps_status='on_time', gps_eta_mins=4, pickup_overdue=True,
             pickup_stalled=True, overdue_mins=7),           # GPS suppresses
        dict(gps_status='watch', gps_eta_mins=12, pickup_overdue=False,
             pickup_stalled=False, overdue_mins=0),
        dict(gps_status='unknown', gps_eta_mins=None, pickup_overdue=True,
             pickup_stalled=False, overdue_mins=4),          # clock watch
        dict(gps_status='', gps_eta_mins=None, pickup_overdue=False,
             pickup_stalled=False, overdue_mins=0),          # clean
    ]

    def test_delegate_matches_promoted_everywhere(self):
        for kw in self.CASES:
            kw = dict(kw, gps_reason='r')
            self.assertEqual(_pickup_risk(**kw), pp.pickup_risk(**kw), msg=kw)

    def test_promoted_precedence_survived(self):
        # The load-bearing rule: fresh on_time GPS outranks the stalled clock.
        r = pp.pickup_risk(pickup_overdue=True, pickup_stalled=True,
                           overdue_mins=7, gps_status='on_time',
                           gps_eta_mins=4, gps_reason='')
        self.assertEqual(r['tier'], '')
        # ...and no signal is not a clean bill of health.
        r = pp.pickup_risk(pickup_overdue=True, pickup_stalled=True,
                           overdue_mins=7, gps_status='unknown',
                           gps_eta_mins=None, gps_reason='')
        self.assertEqual((r['tier'], r['source']), ('critical', 'clock'))


class RevalidateDelegateTests(TestCase):
    """views._revalidate_swap_feasibility must pass straight through to the
    promoted DB wrapper (execute_swap's 409 gate keeps its exact behavior)."""

    def test_views_name_delegates(self):
        from dispatching.views import _revalidate_swap_feasibility
        with patch("dispatching.board_validation.revalidate_moves_against_db",
                   return_value=(False, "sentinel")) as m:
            self.assertEqual(_revalidate_swap_feasibility([(1, 2)], DAY),
                             (False, "sentinel"))
        m.assert_called_once_with([(1, 2)], DAY)

    def test_promoted_wrapper_reports_missing_leg(self):
        preload_timing_cache()
        ok, reason = bv.revalidate_moves_against_db([(999999, 1)], DAY)
        self.assertFalse(ok)
        self.assertIn("999999 not found", reason)


class ValidatePostMoveBoardTests(TestCase):
    """The precise 'no new problems' test."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()

    def _validate(self, schedules, legs_by_id, moves, *, sharer_partners=None,
                  time_changes=None, baseline=None):
        baseline = (bv.board_turn_bands(schedules, DAY)
                    if baseline is None else baseline)
        return bv.validate_post_move_board(
            schedules, legs_by_id, moves, DAY,
            windows={}, sharer_partners=sharer_partners or {},
            baseline_bands=baseline, time_changes=time_changes)

    def test_new_negative_is_hard_rejected(self):
        # Moving the 10:05 job onto the driver who clears his 10:00 at 10:12
        # (+12 reposition) is 19 min short — a NEW negative, rejected.
        a = _leg(101, 10, 0)
        b = _leg(202, 10, 5, pickup_loc="Disney Grand Floridian",
                 dropoff_loc="Disney Boardwalk")
        res = self._validate(_schedules({1: [a], 2: [b]}), _legs_by_id(a, b),
                             [(202, 1)])
        self.assertFalse(res.ok)
        self.assertIn("infeasible", res.reason)

    def test_preexisting_negative_elsewhere_never_vetoes(self):
        # Driver 3 already runs an impossible 10:00/10:05 pair. Fixing something
        # unrelated on driver 1 must not be held hostage to it.
        a = _leg(101, 8, 0)
        b = _leg(202, 16, 0, pickup_loc="Disney Boardwalk",
                 dropoff_loc="Disney Contemporary")
        c = _leg(301, 10, 0)
        d = _leg(302, 10, 5, pickup_loc="Disney Grand Floridian",
                 dropoff_loc="Disney Beach Club")
        scheds = _schedules({1: [a], 2: [b], 3: [c, d]})
        baseline = bv.board_turn_bands(scheds, DAY)
        self.assertEqual(baseline[(3, 301, 302)]["band"], "critical")  # pre-existing
        res = self._validate(scheds, _legs_by_id(a, b, c, d), [(202, 1)],
                             baseline=baseline)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.new_tight_count, 0)

    def test_preexisting_negative_on_the_receiver_does_not_veto_either(self):
        # Even the RECEIVER's own old wound doesn't block: giving driver 3 a
        # comfortable 16:00 job is legal despite his broken morning pair.
        c = _leg(301, 10, 0)
        d = _leg(302, 10, 5, pickup_loc="Disney Grand Floridian",
                 dropoff_loc="Disney Beach Club")
        n = _leg(401, 16, 0, pickup_loc="Disney Boardwalk",
                 dropoff_loc="Disney Contemporary")
        scheds = _schedules({2: [n], 3: [c, d]})
        res = self._validate(scheds, _legs_by_id(c, d, n), [(401, 3)])
        self.assertTrue(res.ok, res.reason)

    def test_tight_demotion_is_recorded_not_rejected(self):
        # 10:30 onto a driver clearing 10:12 (+12 repo) leaves 6 min: legal,
        # but it must be NAMED so the advisor can penalize and disclose it.
        a = _leg(101, 10, 0)
        b = _leg(202, 10, 30, pickup_loc="Disney Grand Floridian",
                 dropoff_loc="Disney Boardwalk")
        res = self._validate(_schedules({1: [a], 2: [b]}), _legs_by_id(a, b),
                             [(202, 1)])
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.new_tight_count, 1)
        self.assertEqual(res.worsened_pairs, [{
            "driver_id": 1, "prev_leg_id": 101, "next_leg_id": 202,
            "before": "", "after": "tight", "slack": 6}])
        self.assertEqual(res.min_buffer_after, 6)
        self.assertEqual(res.per_driver[1]["min_buffer"], 6)

    def test_sharer_conflict_blocks(self):
        # David (1) and Angel (2) split one physical car; Angel holds it
        # 09:00-14:30. A 09:40 leg on David's empty calendar must be refused.
        angel_slot = ScheduleSlot(
            leg_id=999, pickup_time=dt_time(9, 0),
            pickup_location="Disney Contemporary", pickup_category="Disney Resort",
            dropoff_location="MCO", dropoff_category="MCO Terminal",
            trip_type="departure",
            estimated_end_time=_dt(14, 30),
            reservation_id=0, customer_name="", status="in-progress",
            has_flight=False)
        scheds = {
            1: DriverDaySchedule(driver_id=1, driver_name="David",
                                 driver_type="inhouse", slots=[]),
            2: DriverDaySchedule(driver_id=2, driver_name="Angel",
                                 driver_type="inhouse", slots=[angel_slot]),
        }
        x = _leg(501, 9, 40)   # currently unassigned
        res = self._validate(scheds, _legs_by_id(x), [(501, 1)],
                             sharer_partners={1: {2}, 2: {1}})
        self.assertFalse(res.ok)
        self.assertIn("car-share", res.reason)

    def test_retime_demotion_via_time_changes(self):
        # No driver change at all — pulling the 11:00 up to 10:30 in-memory
        # (match_flight simulation) demotes the pair from clean to tight.
        a = _leg(101, 10, 0)
        b = _leg(202, 11, 0, pickup_loc="Disney Grand Floridian",
                 dropoff_loc="Disney Boardwalk")
        scheds = _schedules({1: [a, b]})
        res = self._validate(scheds, _legs_by_id(a, b), [],
                             time_changes={202: dt_time(10, 30)})
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.new_tight_count, 1)
        self.assertEqual(res.worsened_pairs[0]["slack"], 6)
        # ...and the originals were never mutated (read-only contract).
        self.assertEqual(b.pickup_time, dt_time(11, 0))
        self.assertEqual([s.pickup_time for s in scheds[1].slots],
                         [dt_time(10, 0), dt_time(11, 0)])

    def test_unknown_receiver_is_refused(self):
        a = _leg(101, 10, 0)
        res = self._validate(_schedules({1: [a]}), _legs_by_id(a), [(101, 77)])
        self.assertFalse(res.ok)
        self.assertIn("not on the board", res.reason)
