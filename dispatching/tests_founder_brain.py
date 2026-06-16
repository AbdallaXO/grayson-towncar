"""Founder-brain value rules (docs/scheduler-automation/founder-brain-implementation.md).

Covers:
  * leg_value ordering — R3 (booked class, never pax) and R4 (class first, pax second);
  * the class-match guard, both directions — upward (a higher-class driver is barred from
    a lower-class leg when his own class's job would go unassigned) and downward
    (exact-class drivers win the leg over higher-class ones);
  * the evict-to-farm pass (R1+R2) — M1-style displacement, the min-gain gate, departure/
    lock protection, free insertion, flag/bound behavior;
  * the C4 correctness fixes — hotel↔port drive entries and the arrival static floor
    (the sereen 6:01→7:00 admission).

Fixtures are synthetic minimal legs mirroring the 2026-06-14 CSV rows (M1–M5).
"""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

import dispatching.scheduler as sch
from dispatching.scheduler import (
    DRIVE_TIME_ESTIMATES, DriverDaySchedule, ScheduleSlot,
    check_feasibility, evict_to_farm_for_value, leg_value, suggest_assignments,
)

D = date(2026, 6, 14)


class _FakeLeg(SimpleNamespace):
    def get_trip_type(self):
        return self.trip_type

    def get_cruise_direction(self):
        return "from_cruise" if self.trip_type == "cruise" else None

    def is_airport_pickup(self):
        return "MCO" in (self.pickup_location or "") or "SFB" in (self.pickup_location or "")


def _leg(leg_id, pickup_h, pickup_m=0, vtype="suv", trip="arrival", pax=2, revenue=0,
         pickup_loc="MCO Terminal", dropoff_loc="Disney Resort", driver_id=None):
    return _FakeLeg(
        id=leg_id, pickup_time=time(pickup_h, pickup_m),
        pickup_location=pickup_loc, dropoff_location=dropoff_loc,
        effective_vehicle_type=vtype, trip_type=trip,
        revenue_share=revenue, effective_passenger_count=pax,
        effective_luggage_count=0, effective_luggage_type="",
        effective_need_carseats=False, effective_rf_carseats=0,
        effective_ff_carseats=0, effective_booster_seats=0, is_vip=False,
        driver=None, driver_id=driver_id, reservation_id=1, status="pending",
        flight_information=None, reservation=None,
    )


def _slot(leg, target_date=D, end_dt=None):
    return ScheduleSlot(
        leg_id=leg.id, pickup_time=leg.pickup_time,
        pickup_location=leg.pickup_location, pickup_category=leg.pickup_location,
        dropoff_location=leg.dropoff_location, dropoff_category=leg.dropoff_location,
        trip_type=leg.get_trip_type(),
        estimated_end_time=end_dt or sch.estimate_job_end_time(leg, target_date),
        reservation_id=1, customer_name="", status="pending", has_flight=False,
    )


def _sched(driver_id, slots, name=None):
    return DriverDaySchedule(driver_id=driver_id, driver_name=name or f"d{driver_id}",
                             driver_type="inhouse", slots=slots)


class _FakeDriver(SimpleNamespace):
    def __str__(self):
        return self.name

    def get_effective_availability(self, target_date):
        return {"start_hour": 0, "end_hour": 23, "max_hours": None, "flexible": True}


def _driver(did, name=None):
    return _FakeDriver(id=did, name=name or f"d{did}", driver_type="inhouse")


# ════════════════════════════════════════════════════════════════════════════
# C4a — drive table entries
# ════════════════════════════════════════════════════════════════════════════
class DriveTableTests(TestCase):
    def test_hotel_port_pairs_present_at_55(self):
        for a, b in [("Airport Hotel", "Port Canaveral Area"),
                     ("Other Hotel", "Port Canaveral Area")]:
            self.assertEqual(DRIVE_TIME_ESTIMATES[(a, b)], 55)
            self.assertEqual(DRIVE_TIME_ESTIMATES[(b, a)], 55)


# ════════════════════════════════════════════════════════════════════════════
# C1 — leg_value ordering (R3 + R4)
# ════════════════════════════════════════════════════════════════════════════
class LegValueTests(TestCase):
    def test_booked_class_beats_passenger_count(self):
        # R3 / M5: a Van(14 Pax)-class booking with ONE passenger outranks a Van-class
        # booking with eight — revenue and the coverage obligation follow the BOOKED class.
        v14_one_pax = _leg(1, 10, vtype="Van(14 Pax)", pax=1)
        van_eight_pax = _leg(2, 10, vtype="van", pax=8)
        self.assertGreater(leg_value(v14_one_pax), leg_value(van_eight_pax))

    def test_departure_premium_within_class(self):
        # R1: same booked class — the departure (return) outranks the arrival.
        dep = _leg(1, 10, trip="return", pax=2)
        arr = _leg(2, 10, trip="arrival", pax=7)
        self.assertGreater(leg_value(dep), leg_value(arr))

    def test_revenue_never_outranks_trip_type(self):
        rich_arrival = _leg(1, 10, trip="arrival", revenue=5000)
        poor_other = _leg(2, 10, trip="other", revenue=0)
        self.assertGreater(leg_value(poor_other), leg_value(rich_arrival))

    def test_pax_is_final_tiebreak(self):
        # R4 (rizwan): same class, same type, same revenue — higher pax wins.
        three_pax = _leg(1, 10, vtype="towncar", pax=3)
        two_pax = _leg(2, 10, vtype="towncar", pax=2)
        self.assertGreater(leg_value(three_pax), leg_value(two_pax))
        # ...but one revenue dollar outranks any pax difference.
        rich_two_pax = _leg(3, 10, vtype="towncar", pax=2, revenue=1)
        self.assertGreater(leg_value(rich_two_pax), leg_value(three_pax))

    def test_r4_class_first_pax_second(self):
        # R4 (ken): the 2:41 PM Mini Van 4-pax arrival outranks the 2:41 PM towncar 2-pax —
        # and would still outrank a towncar with MORE passengers (class first, never reverse).
        mv4 = _leg(1, 14, 41, vtype="mini_van", pax=4)
        tc2 = _leg(2, 14, 41, vtype="towncar", pax=2)
        tc9 = _leg(3, 14, 41, vtype="towncar", pax=9)
        self.assertGreater(leg_value(mv4), leg_value(tc2))
        self.assertGreater(leg_value(mv4), leg_value(tc9))


# ════════════════════════════════════════════════════════════════════════════
# C4b — arrival clear-time static floor (the sereen 6:01→7:00 hole)
# ════════════════════════════════════════════════════════════════════════════
class ArrivalStaticFloorTests(TestCase):
    """An optimistic flight-ETA/p75 end estimate may not admit a chain the static
    planning model (pickup + 45 dwell + category drive) calls late."""

    def _sereen_pair(self):
        # 6:01 AM MCO→Disney arrival; static floor = 6:01 + 45 + 30 = 7:16.
        arr = _leg(23348, 6, 1, trip="arrival",
                   pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        # 7:00 AM fixed-time departure from the same resort (pax waiting).
        dep = _leg(20423, 7, 0, trip="return",
                   pickup_loc="Disney Resort", dropoff_loc="MCO Terminal")
        return arr, dep

    def _patches(self, optimistic_end):
        return [
            mock.patch("dispatching.scheduler.estimate_job_end_time",
                       lambda leg, td: optimistic_end if leg.get_trip_type() == "arrival"
                       else datetime.combine(D, leg.pickup_time) + timedelta(minutes=30)),
            mock.patch("dispatching.analytics.categorize_location", lambda loc: loc),
            mock.patch("dispatching.scheduler.resolve_drive_minutes", lambda *a, **k: 0),
        ]

    def test_optimistic_arrival_end_cannot_admit_fixed_time_follower(self):
        arr, dep = self._sereen_pair()
        # Decision-time estimate said the arrival clears 6:50 (early-trending flight +
        # thin p75 bucket) — the static model says 7:16, so the 7:00 departure is late.
        patches = self._patches(datetime(2026, 6, 14, 6, 50))
        with patches[0], patches[1], patches[2]:
            sched = _sched(53, [_slot(dep, end_dt=datetime(2026, 6, 14, 7, 30))])
            feas = check_feasibility(sched, arr, D)
        self.assertFalse(feas.feasible)

    def test_flag_off_restores_dynamic_estimate(self):
        arr, dep = self._sereen_pair()
        patches = self._patches(datetime(2026, 6, 14, 6, 50))
        with patches[0], patches[1], patches[2], \
                mock.patch.object(sch, "ARRIVAL_CLEAR_STATIC_FLOOR", False), \
                mock.patch.object(sch, "CHAIN_STATIC_TIMING", False):
            sched = _sched(53, [_slot(dep, end_dt=datetime(2026, 6, 14, 7, 30))])
            feas = check_feasibility(sched, arr, D)
        self.assertTrue(feas.feasible)

    def test_preceding_arrival_floored_for_new_fixed_time_leg(self):
        arr, dep = self._sereen_pair()
        patches = self._patches(datetime(2026, 6, 14, 6, 50))
        with patches[0], patches[1], patches[2]:
            # The arrival is ALREADY on the board with its optimistic 6:50 end; the new
            # leg is the 7:00 departure — the floor must bind in this direction too.
            sched = _sched(53, [_slot(arr, end_dt=datetime(2026, 6, 14, 6, 50))])
            feas = check_feasibility(sched, dep, D)
        self.assertFalse(feas.feasible)

    def test_delayed_flight_keeps_the_later_dynamic_estimate(self):
        arr, dep = self._sereen_pair()
        # Dynamic end 7:40 (delayed flight) is LATER than the 7:16 floor — still binding.
        patches = self._patches(datetime(2026, 6, 14, 7, 40))
        with patches[0], patches[1], patches[2]:
            sched = _sched(53, [])
            sched.slots.append(_slot(arr, end_dt=datetime(2026, 6, 14, 7, 40)))
            feas = check_feasibility(sched, dep, D)
        self.assertFalse(feas.feasible)

    def test_non_arrival_slots_keep_their_estimate(self):
        # A 6:01 'other' job ending 6:50 is NOT floored — the 7:00 pickup stands.
        oth = _leg(1, 6, 1, trip="other", pickup_loc="Disney Resort",
                   dropoff_loc="Disney Resort")
        dep = _leg(2, 7, 0, trip="return", pickup_loc="Disney Resort",
                   dropoff_loc="MCO Terminal")
        patches = self._patches(datetime(2026, 6, 14, 6, 50))
        with patches[0], patches[1], patches[2]:
            sched = _sched(53, [_slot(oth, end_dt=datetime(2026, 6, 14, 6, 50))])
            feas = check_feasibility(sched, dep, D)
        self.assertTrue(feas.feasible)


# ════════════════════════════════════════════════════════════════════════════
# C1 — class-match guard, both directions (R3)
# ════════════════════════════════════════════════════════════════════════════
class ClassMatchGuardUpwardTests(TestCase):
    """Never let the highest-class vehicle run a lower-class job while a same-class job
    at a conflicting time goes unassigned — across hour buckets, and specifically when
    the Pass-0 scarcity rule does NOT fire (exact-type count > reserve_max_scarcity,
    the 6/14 M5 condition: 3+ V14 drivers, so nothing else protects the sprinter job)."""

    def _m5_cross_hour(self, third_window_end=10):
        # Lower-class van leg at 10:00 (clears ~11:15) conflicts with the V14-class leg
        # at 11:00. THREE V14 drivers defeat Pass-0 (exact_count 3 > reserve_max_scarcity
        # 2); drivers 90/91 end their day at `third_window_end`, so driver 64 is the only
        # one who can reach the 11:00 V14 job (unless the test opens 91's window).
        van_leg = _leg(20100, 10, 0, vtype="van", trip="arrival", pax=7,
                       pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        v14_leg = _leg(13398, 11, 0, vtype="Van(14 Pax)", trip="arrival", pax=13,
                       pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        dvtypes = {64: "Van(14 Pax)", 90: "Van(14 Pax)", 91: "Van(14 Pax)"}
        driver_hours = {64: (0, 23), 90: (0, 10), 91: (0, third_window_end)}
        scheds = {did: _sched(did, []) for did in dvtypes}
        return van_leg, v14_leg, scheds, dvtypes, driver_hours

    def _run(self, legs, scheds, dvtypes, driver_hours):
        with mock.patch("dispatching.analytics.categorize_location", lambda loc: loc):
            return {s.leg_id: s.suggested_driver_id
                    for s in suggest_assignments(legs, scheds, D, driver_vtypes=dvtypes,
                                                 driver_hours=driver_hours)}

    def test_v14_driver_protected_for_v14_leg(self):
        van_leg, v14_leg, scheds, dvtypes, dh = self._m5_cross_hour()
        out = self._run([van_leg, v14_leg], scheds, dvtypes, dh)
        self.assertEqual(out[13398], 64)        # the sprinter job keeps its vehicle
        self.assertIn(out[20100], (90, 91))     # the van job rides a window-limited V14

    def test_flag_off_restores_greedy_grab(self):
        van_leg, v14_leg, scheds, dvtypes, dh = self._m5_cross_hour()
        with mock.patch.object(sch, "CLASS_MATCH_GUARD", False):
            out = self._run([van_leg, v14_leg], scheds, dvtypes, dh)
        self.assertEqual(out[20100], 64)        # greedy consumes the V14 driver early...
        self.assertIsNone(out[13398])           # ...and the sprinter job goes unassigned

    def test_guard_releases_when_another_driver_covers_the_class_job(self):
        # Driver 91's window now reaches the 11:00 V14 job → 64 is no longer the only
        # cover, so the guard releases him for the van leg. Both jobs stay covered.
        van_leg, v14_leg, scheds, dvtypes, dh = self._m5_cross_hour(third_window_end=23)
        out = self._run([van_leg, v14_leg], scheds, dvtypes, dh)
        self.assertIsNotNone(out[20100])
        self.assertIsNotNone(out[13398])
        self.assertNotEqual(out[20100], out[13398])


class ClassMatchFirstDownwardTests(TestCase):
    """Push the LOWEST-class jobs onto the lowest-class vehicle: a feasible exact-class
    driver wins the leg even when a higher-class driver scores higher."""

    def _scenario(self):
        tc_leg = _leg(1, 10, 0, vtype="towncar", trip="arrival", pax=2,
                      pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        # SUV driver is perfectly positioned (chains into the pickup: same area, sweet-
        # spot buffer, backward chain) — scores well above the idle towncar driver.
        prior = _leg(9, 8, 30, vtype="suv", trip="return",
                     pickup_loc="Disney Resort", dropoff_loc="MCO Terminal")
        suv_sched = _sched(2, [_slot(prior, end_dt=datetime(2026, 6, 14, 9, 0))])
        scheds = {1: _sched(1, []), 2: suv_sched}
        dvtypes = {1: "towncar", 2: "suv"}
        return tc_leg, scheds, dvtypes

    def _run(self, tc_leg, scheds, dvtypes):
        with mock.patch("dispatching.analytics.categorize_location", lambda loc: loc):
            out = suggest_assignments([tc_leg], scheds, D, driver_vtypes=dvtypes)
        return out[0].suggested_driver_id

    def test_exact_class_driver_wins(self):
        tc_leg, scheds, dvtypes = self._scenario()
        self.assertEqual(self._run(tc_leg, scheds, dvtypes), 1)

    def test_flag_off_lets_score_decide(self):
        tc_leg, scheds, dvtypes = self._scenario()
        with mock.patch.object(sch, "CLASS_MATCH_FIRST", False):
            winner = self._run(tc_leg, scheds, dvtypes)
        self.assertEqual(winner, 2)   # pins that the fixture genuinely inverts on score

    def test_higher_class_still_fallback_when_exact_infeasible(self):
        tc_leg, scheds, dvtypes = self._scenario()
        # Towncar driver blocked by an overlapping job — the SUV must still cover it.
        blocker = _leg(8, 10, 0, vtype="towncar", trip="other",
                       pickup_loc="Disney Resort", dropoff_loc="Disney Resort")
        scheds[1].slots.append(_slot(blocker, end_dt=datetime(2026, 6, 14, 11, 0)))
        self.assertEqual(self._run(tc_leg, scheds, dvtypes), 2)


# ════════════════════════════════════════════════════════════════════════════
# C1 — same-bucket value ordering makes M5 deterministic; R4 same-slot tiebreak
# ════════════════════════════════════════════════════════════════════════════
class ValueOrderingTests(TestCase):
    def _run(self, legs, dvtypes, scheds=None):
        scheds = scheds or {did: _sched(did, []) for did in dvtypes}
        with mock.patch("dispatching.analytics.categorize_location", lambda loc: loc):
            return {s.leg_id: s.suggested_driver_id
                    for s in suggest_assignments(legs, scheds, D, driver_vtypes=dvtypes)}

    def test_m5_same_hour_v14_booking_reaches_v14_driver_first(self):
        # Both 10:00 arrivals tie on (pass, hour, type); the Van(14 Pax)-class booking
        # must reach Raymond's V14 before the Van-class one — pax never enters (13 vs 7
        # would ALSO pick the V14 here, so the class premium is pinned by the 1-pax case).
        v14_leg = _leg(13398, 10, 0, vtype="Van(14 Pax)", trip="arrival", pax=1,
                       pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        van_leg = _leg(20100, 10, 0, vtype="van", trip="arrival", pax=7,
                       pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        out = self._run([van_leg, v14_leg], {64: "Van(14 Pax)"})
        self.assertEqual(out[13398], 64)
        self.assertIsNone(out[20100])

    def test_r4_same_slot_higher_pax_kept_in_house(self):
        # rizwan 6/14: two towncar arrivals two minutes apart; one driver. The 3-pax
        # (10:49) stays in-house, the 2-pax (10:51) farms — pax decides INSIDE the class.
        three = _leg(1, 10, 49, vtype="towncar", trip="arrival", pax=3,
                     pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        two = _leg(2, 10, 51, vtype="towncar", trip="arrival", pax=2,
                   pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        out = self._run([two, three], {55: "towncar"})
        self.assertEqual(out[1], 55)
        self.assertIsNone(out[2])


# ════════════════════════════════════════════════════════════════════════════
# C2 — evict-to-farm pass (R1+R2, fixes M1-M4)
# ════════════════════════════════════════════════════════════════════════════
class EvictToFarmTests(TestCase):
    """M1 shape: ken holds the 9:27 arrival (7 pax); the 10:30 Universal→MCO departure
    (6 pax) is residual and only fits if the arrival is evicted and farmed."""

    def _m1(self, *, arr_trip="arrival", lock_arrival=False):
        ken = _driver(58, "ken")
        # A: 9:27 MCO→Disney arrival, clears ~10:42 (45 dwell + 30 drive).
        A = _leg(11527, 9, 27, vtype="suv", trip=arr_trip, pax=7,
                 pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        # U: 10:30 Universal→MCO departure — infeasible while A is on the board
        # (repo Disney→Universal 28 min lands ~11:10), trivially feasible without it.
        U = _leg(13256, 10, 30, vtype="suv", trip="return", pax=6,
                 pickup_loc="Universal Resort", dropoff_loc="MCO Terminal")
        legs_by_id = {A.id: A, U.id: U}
        fa = {A.id: ken.id}
        locked = {A.id} if lock_arrival else set()
        return ken, A, U, legs_by_id, fa, locked

    def _run(self, drivers, legs_by_id, fa, candidate_ids, locked=None, dvtypes=None):
        with mock.patch("dispatching.analytics.categorize_location", lambda loc: loc):
            return evict_to_farm_for_value(
                fa, candidate_ids, legs_by_id, drivers,
                {d.id: d for d in drivers}, D, dvtypes or {},
                locked_leg_ids=locked)

    def test_m1_arrival_evicted_for_higher_value_departure(self):
        ken, A, U, legs_by_id, fa, locked = self._m1()
        fa, moves = self._run([ken], legs_by_id, fa, [A.id, U.id], locked)
        self.assertEqual(fa, {U.id: ken.id})          # departure seated, arrival farmed
        self.assertEqual(len(moves), 1)
        mv = moves[0]
        self.assertEqual((mv["kind"], mv["leg_id"], mv["evicted_leg_id"]),
                         ("evict", U.id, A.id))
        self.assertIn("evicted", mv["reason"])         # human-readable provenance

    def test_locked_assignment_is_never_evicted(self):
        ken, A, U, legs_by_id, fa, locked = self._m1(lock_arrival=True)
        fa, moves = self._run([ken], legs_by_id, fa, [A.id, U.id], locked)
        self.assertEqual(fa, {A.id: ken.id})
        self.assertEqual(moves, [])

    def test_non_arrival_assignment_is_not_farmable(self):
        # Same shape but the assigned leg is a CRUISE — not the farm currency; no move.
        # (Cruise legs get no dwell, so the conflict is built from a 10:00 pickup:
        # A clears 10:30, +28 repo to Universal lands 10:58 > U's 10:30 pickup.)
        ken = _driver(58, "ken")
        A = _leg(11527, 10, 0, vtype="suv", trip="cruise", pax=7,
                 pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        U = _leg(13256, 10, 30, vtype="suv", trip="return", pax=6,
                 pickup_loc="Universal Resort", dropoff_loc="MCO Terminal")
        legs_by_id = {A.id: A, U.id: U}
        fa, moves = self._run([ken], legs_by_id, {A.id: ken.id}, [A.id, U.id])
        self.assertEqual(fa, {A.id: ken.id})
        self.assertEqual(moves, [])

    def test_min_value_gain_gate_blocks_churn(self):
        # Two same-class arrivals: pax-only difference (gain 0.01 « 500) must not churn.
        ken = _driver(58, "ken")
        A = _leg(1, 9, 27, vtype="suv", trip="arrival", pax=2,
                 pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        U = _leg(2, 9, 30, vtype="suv", trip="arrival", pax=7,
                 pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        fa, moves = self._run([ken], {1: A, 2: U}, {1: ken.id}, [1, 2])
        self.assertEqual(fa, {1: ken.id})
        self.assertEqual(moves, [])

    def test_free_insertion_when_residual_fits_as_is(self):
        ken = _driver(58, "ken")
        U = _leg(23857, 7, 0, vtype="mini_van", trip="other", pax=5,
                 pickup_loc="Disney Resort", dropoff_loc="Disney Resort")
        fa, moves = self._run([ken], {U.id: U}, {}, [U.id])
        self.assertEqual(fa, {U.id: ken.id})
        self.assertEqual(moves[0]["kind"], "free_insert")
        self.assertIsNone(moves[0]["evicted_leg_id"])

    def test_chain_revalidation_blocks_breaking_a_neighbor(self):
        # Evicting A would seat U, but U's clear time collides with ken's NEXT job —
        # the end-to-end revalidation must refuse the move.
        ken, A, U, legs_by_id, fa, locked = self._m1()
        nxt = _leg(99, 11, 5, vtype="suv", trip="return", pax=2,
                   pickup_loc="Disney Resort", dropoff_loc="MCO Terminal")
        legs_by_id[nxt.id] = nxt
        fa[nxt.id] = ken.id
        # U clears MCO ~10:55; repo MCO→Disney 30 min → 11:25 > 11:05 pickup. Infeasible.
        fa, moves = self._run([ken], legs_by_id, fa, [A.id, U.id], locked)
        self.assertNotIn(U.id, fa)
        self.assertEqual([m for m in moves if m["kind"] == "evict"], [])

    def test_flag_off_noop(self):
        ken, A, U, legs_by_id, fa, locked = self._m1()
        with mock.patch.object(sch, "AUTO_EVICT_TO_FARM_PASS", False):
            fa, moves = self._run([ken], legs_by_id, fa, [A.id, U.id], locked)
        self.assertEqual(fa, {A.id: ken.id})
        self.assertEqual(moves, [])

    def test_max_displacements_bound(self):
        # Two independent M1 shapes on two drivers; cap at 1 eviction per run.
        ken, A1, U1, _, _, _ = self._m1()
        rizwan = _driver(55, "rizwan")
        A2 = _leg(201, 9, 27, vtype="suv", trip="arrival", pax=7,
                  pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        U2 = _leg(202, 10, 30, vtype="suv", trip="return", pax=6,
                  pickup_loc="Universal Resort", dropoff_loc="MCO Terminal")
        legs_by_id = {A1.id: A1, U1.id: U1, A2.id: A2, U2.id: U2}
        fa = {A1.id: ken.id, A2.id: rizwan.id}
        fake_cfg = SimpleNamespace(displacement_min_value_gain=500,
                                   max_displacements_per_run=1,
                                   inter_job_buffer=5, arrival_grace_minutes=15)
        with mock.patch("dispatching.models.SchedulerSettings.get_settings",
                        return_value=fake_cfg):
            fa, moves = self._run([ken, rizwan], legs_by_id, fa,
                                  [A1.id, U1.id, A2.id, U2.id])
        self.assertEqual(len([m for m in moves if m["kind"] == "evict"]), 1)

    def test_pre_existing_assignment_never_evicted(self):
        # A is already saved on the leg (driver_id set) and NOT in final_assignments —
        # a pre-existing board assignment is implicitly locked.
        ken, A, U, legs_by_id, fa, locked = self._m1()
        A.driver = ken
        A.driver_id = ken.id
        fa = {}
        fa, moves = self._run([ken], legs_by_id, fa, [U.id], locked)
        self.assertNotIn(U.id, fa)
        self.assertEqual([m for m in moves if m["kind"] == "evict"], [])


# ════════════════════════════════════════════════════════════════════════════
# C3 — type priorities are settings-tunable
# ════════════════════════════════════════════════════════════════════════════
class TypePriorityKnobTests(TestCase):
    def _conflicting_pair(self):
        # One driver; an arrival and a departure that cannot both be served. Whoever is
        # processed first wins the driver; the ordering knob decides.
        arr = _leg(1, 10, 0, vtype="suv", trip="arrival", pax=4,
                   pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        dep = _leg(2, 10, 5, vtype="suv", trip="return", pax=2,
                   pickup_loc="Universal Resort", dropoff_loc="MCO Terminal")
        return arr, dep

    def _run(self, legs):
        scheds = {1: _sched(1, [])}
        with mock.patch("dispatching.analytics.categorize_location", lambda loc: loc):
            return {s.leg_id: s.suggested_driver_id
                    for s in suggest_assignments(legs, scheds, D,
                                                 driver_vtypes={1: "suv"})}

    def test_default_departure_first(self):
        arr, dep = self._conflicting_pair()
        out = self._run([arr, dep])
        self.assertEqual(out[dep.id], 1)
        self.assertIsNone(out[arr.id])

    def test_inverted_priorities_flip_the_order(self):
        arr, dep = self._conflicting_pair()
        from dispatching.models import SchedulerSettings
        real = SchedulerSettings.get_settings()
        fields = {f.name: getattr(real, f.name)
                  for f in real._meta.get_fields()
                  if hasattr(f, 'attname') and f.name != 'id'}
        fields.update(type_priority_arrival=0, type_priority_return=3)
        fake_cfg = SimpleNamespace(**fields)
        with mock.patch("dispatching.models.SchedulerSettings.get_settings",
                        return_value=fake_cfg):
            out = self._run([arr, dep])
        self.assertEqual(out[arr.id], 1)
        self.assertIsNone(out[dep.id])


# ════════════════════════════════════════════════════════════════════════════
# Shared-car gate in the best-fit suggestion engine
# ════════════════════════════════════════════════════════════════════════════
class SharedCarSuggestionGuardTests(TestCase):
    """suggest_assignments() (the engine behind the Unassigned-Jobs 'Best fit' panel and
    the swap-tester page) must not offer a job to a driver whose car-share PARTNER is
    using the one physical unit at that time — even though the driver's OWN calendar is
    free. Reproduces the founder's report: David (006, evenings) gets offered a 09:40 leg
    while Angel (006) is working 09:00–14:30."""

    DAVID, ANGEL = 10, 20

    def _scenario(self):
        # Angel holds a 09:00 → 14:30 job; David is idle in the morning (his real jobs are
        # in the evening, irrelevant to the 09:40 attempt).
        angel_job = _leg(900, 9, 0, vtype="suv", trip="arrival",
                         pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        angel_sched = _sched(self.ANGEL, [_slot(angel_job,
                             end_dt=datetime(2026, 6, 14, 14, 30))], name="Angel")
        scheds = {self.DAVID: _sched(self.DAVID, [], name="David"),
                  self.ANGEL: angel_sched}
        target = _leg(1, 9, 40, vtype="suv", trip="arrival",
                      pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        dvtypes = {self.DAVID: "suv", self.ANGEL: "suv"}
        return target, scheds, dvtypes

    def _run(self, sharer_partners):
        target, scheds, dvtypes = self._scenario()
        with mock.patch("dispatching.analytics.categorize_location", lambda loc: loc):
            out = suggest_assignments([target], scheds, D, driver_vtypes=dvtypes,
                                      sharer_partners=sharer_partners)
        return out[0].suggested_driver_id

    def test_without_sharer_map_david_is_wrongly_offered(self):
        # Baseline / old behavior: David's empty morning makes the 09:40 leg look placeable.
        self.assertEqual(self._run(sharer_partners=None), self.DAVID)

    def test_shared_car_blocks_david(self):
        # With the partner map, David shares Angel's car (busy 09:00–14:30) so the 09:40 leg
        # must NOT be offered to him — and Angel can't take it either, so no driver is suggested.
        self.assertIsNone(
            self._run(sharer_partners={self.DAVID: {self.ANGEL}, self.ANGEL: {self.DAVID}}))
