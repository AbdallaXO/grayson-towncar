"""Guard B' — the minimum turn buffer (founder report 2026-08-09).

The report: the builder seated a 3:30 PM Animal Kingdom departure on a driver whose 2:03 PM
MCO arrival cleared at 3:18 with a 12-min reposition. buffer_minutes came to EXACTLY 0 and
the gate was `< 0`, so zero counted as feasible — the driver was due at his next pickup the
same instant he finished the last one.

These tests use the REAL address strings from the founder's screenshots and run them through
categorize_location, exactly as build_driver_schedules does. That matters: the shared
_slot() fixture stores the CATEGORY name as the address text, which makes
chain_repo_minutes' "same exact address => 0" rule fire and silently zeroes the very
reposition under test.

Classes that call check_feasibility use TestCase, not SimpleTestCase: an ARRIVAL leg reaches
RouteTimingMetric through estimate_job_end_time, so the DB must be available.

Pinned here:
  * the reported pair, both ways (admitted at Aggressive, refused at Standard);
  * the resolution order — per-driver typed number beats the run's mode beats the saved
    default, and None-vs-0 means inherit-vs-opt-out at every level;
  * the founder's same-terminal exemption (drop a return at MCO, grab an MCO arrival);
  * the preceding-slot fix (pick the job that CLEARS last, not the one that STARTS last).
"""
from datetime import datetime

from django.test import SimpleTestCase, TestCase

from dispatching import feasibility_guards as fg
from dispatching.analytics import categorize_location
from dispatching.scheduler import (
    DriverDaySchedule, ScheduleSlot, check_feasibility, driver_min_buffer,
)
from dispatching.tests_founder_brain import _leg, _sched, D

# The founder's real addresses, 2026-08-09.
MCO = "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA"
POFQ = "Disney's Port Orleans Resort - French Quarter, Orleans Drive, Lake Buena Vista, FL, USA"
AKL = "Disney's Animal Kingdom Lodge, Osceola Parkway, Lake Buena Vista, FL, USA"
CORONADO = "Disney's Coronado Springs Resort, World Center Drive, Lake Buena Vista, FL, USA"


def _at(h, m=0):
    return datetime(D.year, D.month, D.day, h, m)


def _cslot(leg, clear_dt):
    """A slot whose categories come from categorize_location (as production does) and whose
    chain clear time is pinned — the gate is under test here, not the estimator."""
    return ScheduleSlot(
        leg_id=leg.id, pickup_time=leg.pickup_time,
        pickup_location=leg.pickup_location,
        pickup_category=categorize_location(leg.pickup_location),
        dropoff_location=leg.dropoff_location,
        dropoff_category=categorize_location(leg.dropoff_location),
        trip_type=leg.get_trip_type(),
        estimated_end_time=clear_dt, chain_clear_dt=clear_dt,
        reservation_id=1, customer_name="", status="pending", has_flight=False,
    )


# A: arrival 2:03 PM, MCO -> Port Orleans French Quarter (booked Van).
#    chain clear = 14:03 + 45 dwell + 30 (MCO Terminal->Disney Resort) = 15:18.
# B: return 3:30 PM, Animal Kingdom Lodge -> MCO (booked Mini Van).
#    reposition = ('Disney Resort','Disney Resort') = 12  =>  earliest 15:30  =>  buffer 0.
FOUNDER_CLEAR_A = _at(15, 18)


def _founder_board():
    leg_a = _leg(1, 14, 3, vtype="van", trip="arrival", pickup_loc=MCO, dropoff_loc=POFQ)
    return _sched(1, [_cslot(leg_a, FOUNDER_CLEAR_A)])


def _founder_leg_b():
    return _leg(2, 15, 30, vtype="mini_van", trip="return", pickup_loc=AKL, dropoff_loc=MCO)


class AddressCategorisationTests(SimpleTestCase):
    """If these drift the whole file is testing something else."""

    def test_real_addresses_bucket_as_expected(self):
        self.assertEqual(categorize_location(MCO), "MCO Terminal")
        self.assertEqual(categorize_location(POFQ), "Disney Resort")
        self.assertEqual(categorize_location(AKL), "Disney Resort")


class FounderPairTests(TestCase):
    def test_pair_computes_exactly_zero_buffer(self):
        """The arithmetic in the report reproduces: 15:18 + 12 == 15:30 == the pickup."""
        feas = check_feasibility(_founder_board(), _founder_leg_b(), D, min_buffer=0)
        self.assertEqual(feas.buffer_minutes, 0)

    def test_aggressive_still_admits_it(self):
        """min_buffer=0 is the pre-2026-08-09 behaviour, byte for byte."""
        feas = check_feasibility(_founder_board(), _founder_leg_b(), D, min_buffer=0)
        self.assertTrue(feas.feasible)
        self.assertIn("Tight: 0min after previous job", feas.warnings)

    def test_default_call_unchanged_for_every_other_caller(self):
        """No min_buffer argument => no buffer. Advisors / farm-out / swap revalidation must
        not silently inherit a planning floor meant for the builder."""
        feas = check_feasibility(_founder_board(), _founder_leg_b(), D)
        self.assertTrue(feas.feasible)
        self.assertEqual(feas.buffer_minutes, 0)

    def test_standard_buffer_refuses_it(self):
        """The reported bug, fixed: at 5 minutes this chain is no longer buildable."""
        self.assertFalse(
            check_feasibility(_founder_board(), _founder_leg_b(), D, min_buffer=5).feasible)

    def test_refusal_reads_as_policy_not_physics(self):
        """A 0-spare turn is possible-but-thin. The reason must say so — a dispatcher who
        reads 'impossible' will go hunting for a bug instead of loosening the buffer."""
        feas = check_feasibility(_founder_board(), _founder_leg_b(), D, min_buffer=5)
        self.assertIn("0min spare", feas.reason)
        self.assertIn("5min buffer", feas.reason)
        self.assertNotIn("more min", feas.reason)

    def test_genuinely_impossible_turn_still_reads_as_physics(self):
        """A negative buffer keeps the old wording regardless of the buffer setting."""
        early = _leg(3, 15, 0, trip="return", pickup_loc=AKL, dropoff_loc=MCO)
        feas = check_feasibility(_founder_board(), early, D, min_buffer=5)
        self.assertFalse(feas.feasible)
        self.assertIn("more min", feas.reason)

    def test_boundary_is_the_buffer_and_nothing_else(self):
        self.assertTrue(check_feasibility(_founder_board(), _founder_leg_b(), D,
                                          min_buffer=0).feasible)
        self.assertFalse(check_feasibility(_founder_board(), _founder_leg_b(), D,
                                           min_buffer=1).feasible)

    def test_fast_driver_override_rebuilds_the_tight_chain(self):
        """The founder's requirement: a driver who genuinely needs no cushion keeps building
        as tight as the drive allows, even on a buffered run."""
        run_buffer = 5
        fast = driver_min_buffer(1, run_buffer, {1: 0})
        normal = driver_min_buffer(2, run_buffer, {1: 0})
        self.assertTrue(check_feasibility(_founder_board(), _founder_leg_b(), D,
                                          min_buffer=fast).feasible)
        self.assertFalse(check_feasibility(_founder_board(), _founder_leg_b(), D,
                                           min_buffer=normal).feasible)


class ResolveMinBufferTests(SimpleTestCase):
    def test_default_is_five(self):
        self.assertEqual(fg.MIN_TURN_BUFFER_DEFAULT, 5)
        self.assertEqual(fg.resolve_min_buffer(), 5)

    def test_modes(self):
        self.assertEqual(fg.BUFFER_MODES["aggressive"], 0)
        self.assertEqual(fg.BUFFER_MODES["standard"], 5)
        self.assertEqual(fg.BUFFER_MODES["relaxed"], 10)

    def test_driver_beats_run_beats_settings(self):
        self.assertEqual(
            fg.resolve_min_buffer(run_buffer=5, driver_buffer=0, settings_buffer=10), 0)
        self.assertEqual(
            fg.resolve_min_buffer(run_buffer=5, driver_buffer=None, settings_buffer=10), 5)
        self.assertEqual(
            fg.resolve_min_buffer(run_buffer=None, driver_buffer=None, settings_buffer=10), 10)

    def test_zero_is_a_real_choice_not_a_missing_one(self):
        """The whole per-driver feature rests on this: a typed 0 must OPT OUT, not fall
        through to the run's value the way a blank does."""
        self.assertEqual(fg.resolve_min_buffer(run_buffer=10, driver_buffer=0), 0)
        self.assertEqual(fg.resolve_min_buffer(run_buffer=10, driver_buffer=None), 10)
        self.assertEqual(fg.resolve_min_buffer(run_buffer=0, settings_buffer=5), 0)

    def test_driver_number_raises_as_well_as_lowers(self):
        """A typed number means it in BOTH directions — a slow driver stays protected even
        on an Aggressive run."""
        self.assertEqual(fg.resolve_min_buffer(run_buffer=0, driver_buffer=15), 15)

    def test_negative_clamps_to_zero(self):
        self.assertEqual(fg.resolve_min_buffer(driver_buffer=-30), 0)

    def test_garbage_falls_through(self):
        self.assertEqual(fg.resolve_min_buffer(driver_buffer="abc", run_buffer=7), 7)

    def test_driver_min_buffer_helper(self):
        self.assertEqual(driver_min_buffer(9, 10, {9: 0}), 0)
        self.assertEqual(driver_min_buffer(9, 10, {9: 20}), 20)
        self.assertEqual(driver_min_buffer(9, 10, {}), 10)
        self.assertEqual(driver_min_buffer(9, 10, None), 10)
        self.assertEqual(driver_min_buffer(9, None, None), 0)


class SameTerminalExemptionTests(TestCase):
    """Founder call 2026-08-09: the buffer must not bind on a same-terminal airport turn."""

    def test_exempt_only_when_both_conditions_hold(self):
        self.assertEqual(
            fg.effective_min_buffer(10, next_is_airport_arrival=True, same_terminal=True), 0)
        self.assertEqual(
            fg.effective_min_buffer(10, next_is_airport_arrival=True, same_terminal=False), 10)
        self.assertEqual(
            fg.effective_min_buffer(10, next_is_airport_arrival=False, same_terminal=True), 10)
        self.assertEqual(
            fg.effective_min_buffer(10, next_is_airport_arrival=False, same_terminal=False), 10)

    def test_zero_buffer_stays_zero(self):
        self.assertEqual(
            fg.effective_min_buffer(0, next_is_airport_arrival=False, same_terminal=False), 0)

    def test_mco_drop_then_mco_arrival_survives_a_relaxed_run(self):
        """The founder's own move: drop a return at MCO at 1:35, grab the 1:34 arrival.
        Guard B' must never be what stops this."""
        ret = _leg(1, 13, 0, trip="return", pickup_loc=POFQ, dropoff_loc=MCO)
        board = _sched(1, [_cslot(ret, _at(13, 35))])
        arr = _leg(2, 13, 34, trip="arrival", pickup_loc=MCO, dropoff_loc=AKL)
        self.assertTrue(check_feasibility(board, arr, D, min_buffer=10).feasible)

    def test_reposition_into_an_arrival_is_not_exempt(self):
        """Driving in from a resort, not standing at the gate — the cushion applies."""
        prev = _leg(1, 12, 0, trip="return", pickup_loc=AKL, dropoff_loc=POFQ)
        board = _sched(1, [_cslot(prev, _at(13, 0))])
        # Disney Resort -> MCO Terminal is 30 min, so 13:30 is exactly 0 spare.
        arr = _leg(2, 13, 30, trip="arrival", pickup_loc=MCO, dropoff_loc=AKL)
        self.assertTrue(check_feasibility(board, arr, D, min_buffer=0).feasible)
        self.assertFalse(check_feasibility(board, arr, D, min_buffer=5).feasible)


class PrecedingSlotTests(TestCase):
    """B5 — preceding must be the job that CLEARS last, not the one that STARTS last."""

    def test_long_earlier_job_is_not_masked_by_a_short_later_one(self):
        # 10:00 job clearing 14:00 (long) and an 11:00 job clearing 11:30 (short). A 12:00
        # pickup conflicts with the FIRST; selecting by pickup time compares against the
        # 11:30 clear and waves it through.
        long_leg = _leg(1, 10, 0, trip="return", pickup_loc=MCO, dropoff_loc=AKL)
        short_leg = _leg(2, 11, 0, trip="return", pickup_loc=CORONADO, dropoff_loc=POFQ)
        board = DriverDaySchedule(
            driver_id=1, driver_name="d1", driver_type="inhouse",
            slots=[_cslot(long_leg, _at(14, 0)), _cslot(short_leg, _at(11, 30))])
        new = _leg(3, 12, 0, trip="return", pickup_loc=CORONADO, dropoff_loc=MCO)
        feas = check_feasibility(board, new, D, min_buffer=0)
        self.assertFalse(feas.feasible, "must measure against the 2:00 PM clear, not 11:30")
        self.assertIn("more min", feas.reason)

    def test_valid_chain_is_unaffected(self):
        """Where the chain is already sane both selections agree — no behaviour change."""
        a = _leg(1, 8, 0, trip="return", pickup_loc=AKL, dropoff_loc=POFQ)
        b = _leg(2, 10, 0, trip="return", pickup_loc=POFQ, dropoff_loc=CORONADO)
        board = DriverDaySchedule(
            driver_id=1, driver_name="d1", driver_type="inhouse",
            slots=[_cslot(a, _at(8, 40)), _cslot(b, _at(10, 40))])
        new = _leg(3, 13, 0, trip="return", pickup_loc=AKL, dropoff_loc=MCO)
        self.assertTrue(check_feasibility(board, new, D, min_buffer=10).feasible)
