"""Unit tests for the gap-compaction relocation pass (scheduler.compact_gaps_via_relocation).

The pass relocates an already-covered leg from a donor driver to a driver with a big internal
gap, when it heals more gap than it opens (the founder's "give David the 6:15 Roberto holds;
Roberto just starts later" move). These tests exercise the DECISION logic — gap detection, the
net-gain accept rule, first/last-vs-middle preference, locked legs, infeasible inserts, and
coverage preservation. The underlying primitives it leans on (build_driver_schedules,
check_feasibility, estimate_job_end_time, resolve_drive_minutes) are tested elsewhere and are
patched here so the scenarios are exact and DB-free.
"""
from contextlib import ExitStack
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from dispatching.scheduler import (
    compact_gaps_via_relocation, _max_internal_gap_minutes,
    FeasibilityResult, DriverDaySchedule, ScheduleSlot,
)

TD = date(2026, 6, 2)


# ── lightweight fakes (no DB) ────────────────────────────────────────────────

class FakeLeg:
    def __init__(self, lid, pickup, dur=45, vtype=None):
        self.id = lid
        self.pickup_time = pickup
        self.dur = dur
        self.effective_vehicle_type = vtype
        self.pickup_location = f"PU{lid}"
        self.dropoff_location = f"DO{lid}"
        self.driver = None
        self.driver_id = None


class FakeDriver:
    def __init__(self, did, flexible=True):
        self.id = did
        self._flex = flexible

    def __str__(self):
        return f"Driver{self.id}"

    def get_effective_availability(self, target_date):
        return {"start_hour": 0, "end_hour": 23, "max_hours": None, "flexible": self._flex}


def _end(leg, target_date):
    return datetime.combine(target_date, leg.pickup_time) + timedelta(minutes=leg.dur)


def _fake_build(legs, drivers, target_date):
    sch = {d.id: DriverDaySchedule(d.id, str(d), "inhouse", []) for d in drivers}
    for leg in legs:
        if leg.driver_id in sch:
            sch[leg.driver_id].slots.append(ScheduleSlot(
                leg_id=leg.id, pickup_time=leg.pickup_time, pickup_location=leg.pickup_location,
                pickup_category=leg.pickup_location, dropoff_location=leg.dropoff_location,
                dropoff_category=leg.dropoff_location, trip_type="other",
                estimated_end_time=_end(leg, target_date), reservation_id=0, customer_name="",
                status="in-progress", has_flight=False, vehicle_type=leg.effective_vehicle_type))
    for s in sch.values():
        s.slots.sort(key=lambda x: x.pickup_time)
    return sch


def _slot(lid, pickup, end):
    return ScheduleSlot(
        leg_id=lid, pickup_time=pickup, pickup_location="P", pickup_category="P",
        dropoff_location="D", dropoff_category="D", trip_type="other",
        estimated_end_time=end, reservation_id=0, customer_name="", status="x", has_flight=False)


def _run(legs, drivers, board, locked=None, feasible=True, dvtypes=None):
    """Run the pass with all DB-touching primitives patched. Returns (final_assignments, moves)."""
    legs_by_id = {l.id: l for l in legs}
    drivers_by_id = {d.id: d for d in drivers}
    feas = FeasibilityResult(feasible=feasible, buffer_minutes=30 if feasible else -1)
    with ExitStack() as es:
        es.enter_context(mock.patch("dispatching.scheduler.build_driver_schedules", _fake_build))
        es.enter_context(mock.patch("dispatching.scheduler.estimate_job_end_time", _end))
        es.enter_context(mock.patch("dispatching.scheduler.resolve_drive_minutes", lambda *a, **k: 10))
        es.enter_context(mock.patch("dispatching.scheduler.check_feasibility", lambda *a, **k: feas))
        es.enter_context(mock.patch("dispatching.feasibility_guards.get_effective_window", lambda *a, **k: None))
        es.enter_context(mock.patch("dispatching.analytics.categorize_location", lambda loc: loc))
        es.enter_context(mock.patch("dispatching.models.SchedulerSettings.get_settings",
                                    return_value=SimpleNamespace(inter_job_buffer=5, arrival_grace_minutes=15)))
        return compact_gaps_via_relocation(
            dict(board), legs_by_id, drivers, drivers_by_id, TD, dvtypes=dvtypes or {},
            locked_leg_ids=locked,
        )


class MaxInternalGapTests(SimpleTestCase):
    def test_empty_and_single(self):
        self.assertEqual(_max_internal_gap_minutes([], TD), 0)
        self.assertEqual(_max_internal_gap_minutes([_slot(1, time(6, 0), datetime(2026, 6, 2, 6, 45))], TD), 0)

    def test_picks_largest_gap(self):
        slots = [
            _slot(1, time(6, 0), datetime(2026, 6, 2, 6, 45)),
            _slot(2, time(8, 0), datetime(2026, 6, 2, 8, 45)),
            _slot(3, time(9, 0), datetime(2026, 6, 2, 9, 45)),
        ]
        # gaps: 6:45->8:00 = 75, 8:45->9:00 = 15  => 75
        self.assertEqual(_max_internal_gap_minutes(slots, TD), 75)


class FirstJobDonorTests(SimpleTestCase):
    """The David/Roberto case: pull the donor's FIRST job to split the receiver's big hole."""

    def _scenario(self):
        # Receiver D1 (David): 3:30 + 8:30 -> a 255-min hole between them.
        # Donor D2 (Roberto): 6:15 (his first job, sits in David's hole) + 9:00/11:00/13:00, so he
        # has > 3 jobs and isn't a protected light donor; donating 6:15 just makes him start later.
        legs = [FakeLeg(1, time(3, 30)), FakeLeg(2, time(8, 30)),
                FakeLeg(3, time(6, 15)), FakeLeg(4, time(9, 0)),
                FakeLeg(5, time(11, 0)), FakeLeg(6, time(13, 0))]
        drivers = [FakeDriver(1), FakeDriver(2)]
        board = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 2}
        return legs, drivers, board

    def test_first_job_moves_to_fill_hole(self):
        legs, drivers, board = self._scenario()
        final, moves = _run(legs, drivers, board)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["leg_id"], 3)
        self.assertEqual((moves[0]["from_driver_id"], moves[0]["to_driver_id"]), (2, 1))
        self.assertEqual(moves[0]["donor_gap_opened_min"], 0)  # Roberto just starts later
        self.assertEqual(final[3], 1)

    def test_coverage_preserved(self):
        legs, drivers, board = self._scenario()
        before = set(board)
        final, moves = _run(legs, drivers, board)
        self.assertTrue(moves)
        self.assertEqual(set(final), before)        # same legs, none dropped/farmed
        self.assertEqual(len(final), 6)

    def test_locked_leg_never_moved(self):
        legs, drivers, board = self._scenario()
        final, moves = _run(legs, drivers, board, locked={3})
        self.assertEqual(moves, [])
        self.assertEqual(final[3], 2)

    def test_infeasible_insert_rejected(self):
        legs, drivers, board = self._scenario()
        final, moves = _run(legs, drivers, board, feasible=False)
        self.assertEqual(moves, [])
        self.assertEqual(final[3], 2)


class MiddleJobTests(SimpleTestCase):
    def test_middle_job_rejected_when_it_opens_a_bigger_gap(self):
        # Receiver D1 hole 7:50->9:55 (125 min); only the donor's MIDDLE job (8:30) lands in it
        # (donor 7:00/10:00/11:00 fall outside the hole). The donor has 4 jobs (not protected),
        # so this exercises the NET-GAIN rule: removing the middle opens a 135-min hole on the
        # donor (heal 85 - open 90 = net -5) -> reject.
        legs = [FakeLeg(1, time(6, 20), dur=90), FakeLeg(2, time(9, 55)),     # receiver: end 7:50, 9:55
                FakeLeg(3, time(7, 0)), FakeLeg(4, time(8, 30)),
                FakeLeg(5, time(10, 0)), FakeLeg(6, time(11, 0))]            # donor (4 jobs)
        drivers = [FakeDriver(1), FakeDriver(2)]
        board = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 2}
        final, moves = _run(legs, drivers, board)
        self.assertEqual(moves, [])

    def test_middle_job_accepted_when_donor_has_a_dominating_gap_elsewhere(self):
        # Receiver D1 hole 6:45->12:00 (315 min). Donor D2 has an evening job at 20:00, so its
        # largest gap (10:40->20:00) dominates; removing the 9:00 MIDDLE job doesn't grow it
        # (opens 0) while it heals 180 min of the receiver's hole -> accept.
        legs = [FakeLeg(1, time(6, 0)), FakeLeg(2, time(12, 0)),               # receiver
                FakeLeg(3, time(5, 0)), FakeLeg(4, time(9, 0)),
                FakeLeg(5, time(9, 55)), FakeLeg(6, time(20, 0))]              # donor (4 = middle)
        drivers = [FakeDriver(1), FakeDriver(2)]
        board = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 2}
        final, moves = _run(legs, drivers, board)
        # The middle job (4) is the best move and is relocated; it opens no donor gap because
        # the donor's evening (10:40->20:00) gap dominates. We assert it moves rather than that
        # it's the only move — once a big hole is split, the pass may make further net-positive
        # compactions, each bounded by the move-once lock.
        self.assertTrue(moves)
        self.assertEqual(moves[0]["leg_id"], 4)                # the middle job, not a boundary one
        self.assertEqual((moves[0]["from_driver_id"], moves[0]["to_driver_id"]), (2, 1))
        self.assertEqual(moves[0]["donor_gap_opened_min"], 0)
        self.assertEqual(final[4], 1)


class CalibrationTests(SimpleTestCase):
    def test_light_donor_protected(self):
        # Donor D2 has only 3 jobs (6:15/9:00/11:00) and the 6:15 would perfectly fill D1's hole,
        # but a <=3-job donor is protected -> no move (keep light drivers' work intact).
        legs = [FakeLeg(1, time(3, 30)), FakeLeg(2, time(8, 30)),
                FakeLeg(3, time(6, 15)), FakeLeg(4, time(9, 0)), FakeLeg(5, time(11, 0))]
        drivers = [FakeDriver(1), FakeDriver(2)]
        board = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2}
        final, moves = _run(legs, drivers, board)
        self.assertEqual(moves, [])
        self.assertEqual(final[3], 2)

    def test_prefers_tier_matched_receiver(self):
        # A towncar leg (9:00) sits in a hole on BOTH a towncar driver (D1) and a van driver (D2).
        # It should go to the tier-matched towncar driver, not waste the scarce van.
        legs = [FakeLeg(1, time(6, 0)), FakeLeg(2, time(12, 0)),                  # D1 towncar: hole 6:45->12:00
                FakeLeg(3, time(6, 30)), FakeLeg(4, time(12, 30)),               # D2 van: hole 7:15->12:30
                FakeLeg(10, time(9, 0), vtype="towncar"),                        # the leg to move (on D3)
                FakeLeg(5, time(14, 0)), FakeLeg(6, time(15, 0)), FakeLeg(7, time(16, 0))]  # D3 fillers
        drivers = [FakeDriver(1), FakeDriver(2), FakeDriver(3)]
        board = {1: 1, 2: 1, 3: 2, 4: 2, 10: 3, 5: 3, 6: 3, 7: 3}
        dvtypes = {1: "towncar", 2: "van", 3: "towncar"}
        final, moves = _run(legs, drivers, board, dvtypes=dvtypes)
        self.assertTrue(moves)
        self.assertEqual(moves[0]["leg_id"], 10)
        self.assertEqual(moves[0]["to_driver_id"], 1)   # towncar driver, not the van (2)
        self.assertEqual(final[10], 1)
