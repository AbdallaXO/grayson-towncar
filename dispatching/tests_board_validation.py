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
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from dispatching import board_validation as bv
from dispatching import pickup_policy as pp
from dispatching.scheduler import (
    DriverDaySchedule, ScheduleSlot, _make_sim_slot, preload_timing_cache,
)
from dispatching.tests_swap_guards import fake_leg
from dispatching.views import _gap_turn_slack, _pickup_risk
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle as RateVehicle
from reservations.models import Customer, Leg, Reservation

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


class TakesLaterParityTests(TestCase):
    """CHAIN_CLEAR_TAKES_LATER on the LIVE path (2026-09-05).

    scheduler.check_feasibility has, since 2026-09-02, refused to plan a chain on a
    clear time earlier than the board's own measured estimate when the job being seated
    is FIXED-TIME. turn_slack_minutes did not, so for three days the board could show a
    clean chip on a turn the engine would refuse to build — analysis/24 measured 5.1 of
    them a day on real boards, worst case a CLEAN chip on a turn 47 minutes short.

    What is pinned here: the rule fires for fixed-time next jobs, is exempt for airport
    arrivals (the guest is still deplaning), and never touches the recorded-pickup
    branch, where the clear time is a fact rather than a model estimate.
    """

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()

    def _slots(self, next_trip="return"):
        """Built through the same sim-slot builder the engine uses, so the location
        CATEGORIES are the real ones, then given the shape 22 found on real boards: a
        MEASURED end (3:40) running 25 min past the static model's clear (3:15).

        The next job picks up at MCO either way, so switching next_trip between
        'return' and 'arrival' switches the rule's exemption and nothing else —
        is_airport_arrival reads the trip type AND the pickup category."""
        prev = _make_sim_slot(_leg(201, 14, 0, trip="arrival",
                                   pickup_loc="MCO Terminal B",
                                   dropoff_loc="Disney Contemporary"), DAY)
        prev.chain_clear_dt = _dt(15, 15)
        prev.estimated_end_time = _dt(15, 40)
        nxt = _make_sim_slot(_leg(202, 16, 0, trip=next_trip,
                                  pickup_loc="MCO Terminal A",
                                  dropoff_loc="Disney Boardwalk"), DAY)
        return prev, nxt

    def _slack_without_the_rule(self, prev, nxt):
        with patch("dispatching.board_validation.CHAIN_CLEAR_TAKES_LATER", False,
                   create=True):
            from dispatching import scheduler as sch
            with patch.object(sch, "CHAIN_CLEAR_TAKES_LATER", False):
                return bv.turn_slack_minutes(prev, nxt, DAY)

    def test_fixed_time_next_job_takes_the_later_clear(self):
        prev, nxt = self._slots(next_trip="return")
        before = self._slack_without_the_rule(prev, nxt)
        after = bv.turn_slack_minutes(prev, nxt, DAY)
        # The measured clear is 25 min past the static one, and the whole 25 comes
        # off the slack the dispatcher is shown.
        self.assertEqual(before - after, 25)

    def test_airport_arrival_next_job_is_exempt(self):
        prev, nxt = self._slots(next_trip="arrival")
        # The guest is still deplaning, so the arrival keeps the static clear and the
        # chip does not move at all.
        self.assertEqual(bv.turn_slack_minutes(prev, nxt, DAY),
                         self._slack_without_the_rule(prev, nxt))

    def test_recorded_pickup_still_beats_the_model(self):
        """A fact must never be raised to a model estimate: a driver who demonstrably
        picked up early keeps his real clear time."""
        prev, nxt = self._slots(next_trip="return")
        prev_leg = _leg(201, 14, 0, trip="arrival",
                        pickup_loc="MCO Terminal B",
                        dropoff_loc="Disney Contemporary")
        on_fact = bv.turn_slack_minutes(prev, nxt, DAY, prev_leg=prev_leg,
                                        prev_picked_up_dt=_dt(14, 0))
        self.assertIsNotNone(on_fact)
        self.assertGreater(on_fact, bv.turn_slack_minutes(prev, nxt, DAY))

    def test_the_board_can_no_longer_call_an_impossible_turn_clean(self):
        """analysis/24's finding, as a regression: a turn the engine refuses must not
        band clean or tight on the live path. This is the sereen 2026-07-25 shape — a
        CLEAN chip on a turn 47 minutes short."""
        prev, nxt = self._slots(next_trip="return")
        prev.chain_clear_dt = _dt(14, 30)         # static model: hours of room
        prev.estimated_end_time = _dt(16, 5)      # measured: clears AFTER the pickup
        self.assertEqual(pp.turn_band(self._slack_without_the_rule(prev, nxt)), "")
        slack = bv.turn_slack_minutes(prev, nxt, DAY)
        self.assertLess(slack, 0)
        self.assertEqual(pp.turn_band(slack), "critical")


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


class RevalidateSharerScopeTests(TestCase):
    """revalidate_moves_against_db's car-share partner map must see every
    ROSTERED co-holder of a shared vehicle, not just the ones who already
    hold a leg — 2026-08-24 fix (05_BUILD3B_TICKETS.md §9.2 item 3).

    build_sharer_partners() silently returns {} for a unit whose co-holder
    isn't in the id set it's given (car_share.py's own docstring). The old
    code built that set from `{l.driver_id for l in legs if l.driver_id}` —
    a driver rostered (DVA row) but not yet holding any leg that day was
    invisible. Traced through by hand before writing this: because the
    function (a) re-queries ALL of the date's legs, not just the ones being
    moved, and (b) applies every move in `valid_moves` before deriving that
    set, a co-holder who is truly legless has nothing to conflict against
    regardless — so this fix does not flip any REACHABLE verdict in this
    caller today. What it fixes is real: the partner map it hands to
    sharers_conflict() is now complete, which is what any future move batch
    or reader of that map is entitled to assume. Test the map, not a verdict
    that provably can't differ."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        vtype = RateVehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        rate = Rate.objects.create(
            route=route, vehicle=vtype,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.route = route
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user(username="b_driver", first_name="B"),
            driver_type="inhouse")
        cls.legless_partner = Driver.objects.create(
            profile=User.objects.create_user(username="d_driver", first_name="D"),
            driver_type="inhouse")
        customer = Customer.objects.create(
            first_name="Pat", last_name="Guest", email="pat@example.com",
            phone_number="5550001111")
        cls.reservation = Reservation.objects.create(
            trip_type="one-way", customer=customer, vehicle=vtype, rate=rate,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        car = FleetVehicle.objects.create(
            vehicle_number="014", year=2023, make="Chevrolet", model="Suburban",
            vehicle_type=vtype)
        DriverVehicleAssignment.objects.create(driver=cls.driver, date=DAY, vehicle=car)
        DriverVehicleAssignment.objects.create(
            driver=cls.legless_partner, date=DAY, vehicle=car)  # rostered, ZERO legs

    def test_rostered_legless_coholder_is_included_as_a_partner(self):
        leg = Leg.objects.create(
            reservation=self.reservation, pickup_date=DAY, pickup_time=dt_time(9, 0),
            pickup_location="Disney Contemporary", dropoff_location="MCO",
            route=self.route)
        from dispatching import car_share as cs
        with patch("dispatching.scheduler.build_sharer_partners",
                   wraps=cs.build_sharer_partners) as spy:
            bv.revalidate_moves_against_db([(leg.id, self.driver.id)], DAY)
        called_with_ids = spy.call_args[0][0]
        self.assertIn(self.legless_partner.id, called_with_ids,
                      "the legless co-holder must still be visible to "
                      "build_sharer_partners so the partnership itself is "
                      "recorded, even though nothing conflicts with an "
                      "empty schedule today")

    def test_unrostered_uninvolved_driver_still_excluded(self):
        # A driver who is neither rostered on this car nor part of the move
        # must NOT be pulled in — the fix widens scope to the ROSTER, it
        # does not widen it to every driver in the system.
        other = Driver.objects.create(
            profile=User.objects.create_user(username="e_driver", first_name="E"),
            driver_type="inhouse")
        leg = Leg.objects.create(
            reservation=self.reservation, pickup_date=DAY, pickup_time=dt_time(9, 0),
            pickup_location="Disney Contemporary", dropoff_location="MCO",
            route=self.route)
        from dispatching import car_share as cs
        with patch("dispatching.scheduler.build_sharer_partners",
                   wraps=cs.build_sharer_partners) as spy:
            bv.revalidate_moves_against_db([(leg.id, self.driver.id)], DAY)
        called_with_ids = spy.call_args[0][0]
        self.assertNotIn(other.id, called_with_ids)
