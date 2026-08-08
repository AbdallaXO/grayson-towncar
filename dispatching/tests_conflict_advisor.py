"""The Recovery Advisor engine: dispatching/conflict_advisor.py — the board
fingerprint, the guard-1 clock matrix, disruption DETECTION (Stage B1), and
candidate GENERATION / whole-board validation / ranking / the public
compute_advisor_state contract (Stage B2).

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_conflict_advisor

What is pinned here and why (each maps to a plan hazard guard):
  * PRIME-DIRECTIVE NET (guard 13, the most important test in the suite): a
    founder-built tight day (+0/+3 buffer patterns) produces ZERO cards. The
    advisor cards what breaks or what reality degraded — never a day the
    founder deliberately built tight.
  * Recorded pickup re-anchors the chain (guard 1): an early pickup makes a
    planning-impossible turn fine — no false card.
  * GPS discipline (guard 4): fresh on_time suppresses the clock; stale GPS
    yields to the clock; stale-but-parked NEVER generates a disruption.
  * Overdue-stale (guard 2): past OVERDUE_STALE_MIN it's a hygiene card, never
    a recovery disruption.
  * Delayed flight is not "late": it relaxes the turn INTO the arrival and the
    card for the broken turn OUT is the flight_change CAUSE, not an overlap
    symptom.
  * Overnight (guard 3): a 23:30 → 00:15 same-board pair is never a false
    overlap; a genuine tonight→tail chain break IS caught across midnight via
    prev_tail; an overnight-AMBIGUOUS arrival abstains ("confirm the date").
  * Fingerprint (guard 11): 3 queries, stable on identical state, bumps on a
    LegStatus insert / roster row / pickup move.

Detection fixtures are synthetic in-memory boards (tests_founder_brain style —
SimpleNamespace legs + _make_sim_slot), so everything runs without a DB row;
only the fingerprint tests touch the DB.
"""
from datetime import date as dt_date, datetime, time as dt_time, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from dispatching import conflict_advisor as ca
from dispatching.scheduler import (
    DriverDaySchedule, _make_sim_slot, chain_clear_dt,
    chain_clear_dt_from_actual, preload_timing_cache,
)

User = get_user_model()

DAY = dt_date(2026, 5, 1)
PREV = DAY - timedelta(days=1)


def _dt(h, m=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m)


class _FakeFlight(SimpleNamespace):
    def best_arrival_local(self):
        return self.estimated_gate_arrival_local

    def get_flight_ident(self):
        return self.ident


def _flight(arrival_dt, ident="DL123", scheduled=None):
    """``arrival_dt`` is the LIVE estimate (what the plane is now doing);
    ``scheduled`` is its published timetable — the baseline the advisor
    measures movement against. Passing scheduled == arrival_dt models a flight
    running exactly on time, which must never raise a card however far the
    booking was written from it."""
    return _FakeFlight(
        ident=ident,
        actual_gate_arrival_local=None,
        estimated_gate_arrival_local=arrival_dt,
        actual_arrival_local=None,
        estimated_arrival_local=None,
        scheduled_gate_arrival_local=scheduled,
        scheduled_arrival_local=None,
    )


class _FakeLeg(SimpleNamespace):
    def get_trip_type(self):
        return self.trip_type

    def get_cruise_direction(self):
        if self.trip_type != "cruise":
            return None
        return "to_cruise" if self.is_airport_pickup() else "from_cruise"

    def is_airport_pickup(self):
        loc = self.pickup_location or ""
        return "MCO" in loc or "SFB" in loc

    def is_flight_tracked_arrival(self):
        return (self.trip_type == "arrival"
                or (self.trip_type == "cruise" and self.is_airport_pickup()))


def _leg(leg_id, hh, mm=0, trip="other", pickup="Disney Contemporary",
         dropoff="Disney Polynesian", status="confirmed", driver_id=1,
         flight=None, day=DAY, **kw):
    defaults = dict(
        id=leg_id, pickup_date=day, pickup_time=dt_time(hh, mm),
        pickup_location=pickup, dropoff_location=dropoff,
        trip_type=trip, status=status, driver_id=driver_id,
        reservation=None, reservation_id=1,
        flight_information=flight, controlling_flight=flight,
        dispatch_risk_status="", dispatch_eta_minutes=None,
        dispatch_risk_reason="", dispatch_eta_target="",
        dispatch_eta_target_time=None, dispatch_eta_is_fresh=False,
        dispatch_is_moving=None,
        pickup_time_changed_at=None, pickup_change_ack_at=None,
        pickup_time_was=None, has_unacked_time_change=False,
        overnight_confirmed_at=None,
        effective_passenger_count=2, effective_luggage_count=2,
        revenue_share=None,
    )
    defaults.update(kw)
    return _FakeLeg(**defaults)


def _board(assignment, now, picked=None, extra_legs=None, prev_tail=None,
           tasks=None, day=DAY, vip=None, vtypes=None, sharers=None,
           drivers=None, refunds=None, keoi=None, windows=None,
           window_sources=None, farm_ctx="none"):
    """{driver_id: [legs]} -> BoardState, via the same sim-slot builder the
    engine uses. `extra_legs` are on the board but not in any schedule
    (unassigned / affiliate-held). Generation-facing fields default sane:
    every schedule driver gets a windows entry (None = Guard C skipped),
    baseline bands are the planning-clock sweep (as build_board_state does),
    and the lazy farm context is pre-marked built (None = no carded
    affiliate) so pure tests never touch the roster tables — pass
    farm_ctx=<obj> to inject a fake pricing context, farm_ctx="lazy" to let
    the engine resolve it from the DB."""
    from dispatching.board_validation import board_turn_bands

    all_legs, schedules = [], {}
    for did, legs in assignment.items():
        slots = sorted((_make_sim_slot(l, day) for l in legs),
                       key=lambda s: (s.pickup_time, s.leg_id))
        schedules[did] = DriverDaySchedule(
            driver_id=did, driver_name=f"D{did}", driver_type="inhouse",
            slots=list(slots))
        all_legs.extend(legs)
    all_legs.extend(extra_legs or [])
    eff_windows = {did: None for did in schedules}
    eff_windows.update(windows or {})
    board = ca.BoardState(
        target_date=day, now=timezone.make_aware(now), now_local=now,
        legs=sorted(all_legs, key=lambda l: (l.pickup_time, l.id)),
        legs_by_id={l.id: l for l in all_legs},
        schedules=schedules,
        drivers_by_id=drivers or {},
        windows=eff_windows,
        window_sources=window_sources or {},
        driver_vtypes=vtypes or {},
        sharer_partners=sharers or {},
        picked_up_by_leg=picked or {},
        prev_tail=prev_tail or {},
        open_tasks_by_leg=tasks or {},
        vip_leg_ids=vip or set(),
        pending_refund_leg_ids=refunds or set(),
        keoi_leg_ids=keoi or set(),
        baseline_bands=board_turn_bands(schedules, day),
    )
    if farm_ctx != "lazy":
        board._farm_ctx = None if farm_ctx == "none" else farm_ctx
        board._farm_ctx_built = True
    return board


class _PureBoardTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()  # empty test DB -> founder tables, no per-call DB


# ════════════════════════════════════════════════════════════════════════════
# advisor_clear_dt — the guard-1 clock matrix
# ════════════════════════════════════════════════════════════════════════════
class AdvisorClearDtTests(_PureBoardTestCase):
    def _arrival(self, status="confirmed"):
        return _leg(1, 14, 0, trip="arrival", pickup="MCO Terminal B",
                    dropoff="Disney Contemporary", status=status)

    def test_future_leg_uses_static_chain_clock(self):
        leg = self._arrival()
        self.assertEqual(ca.advisor_clear_dt(leg, DAY),
                         chain_clear_dt(leg, DAY))          # 15:15
        self.assertEqual(ca.advisor_clear_dt(leg, DAY), _dt(15, 15))

    def test_picked_up_detection_believes_the_fact_planning_never_optimistic(self):
        leg = self._arrival(status="picked-up")
        early = _dt(14, 5)   # tapped picked-up 14:05 -> dwell is spent
        det = ca.advisor_clear_dt(leg, DAY, picked_up_dt=early)
        self.assertEqual(det, chain_clear_dt_from_actual(leg, early))  # 14:35
        self.assertEqual(det, _dt(14, 35))
        # Planning clock: max(static, actual) — an early fact never lets the
        # planner seat work before the protective static clear.
        self.assertEqual(
            ca.advisor_clear_dt(leg, DAY, picked_up_dt=early, mode="planning"),
            _dt(15, 15))
        # A LATE fact pushes BOTH clocks (reality beats the plan both ways).
        late = _dt(16, 0)
        self.assertEqual(
            ca.advisor_clear_dt(leg, DAY, picked_up_dt=late, mode="planning"),
            _dt(16, 30))

    def test_on_the_way_dwell_not_yet_a_fact(self):
        leg = self._arrival(status="on-the-way")
        self.assertEqual(ca.advisor_clear_dt(leg, DAY), _dt(15, 15))

    def test_picked_up_status_without_recorded_tap_stays_static(self):
        leg = self._arrival(status="picked-up")
        self.assertEqual(ca.advisor_clear_dt(leg, DAY), _dt(15, 15))

    def test_completed_and_cancelled_are_dropped(self):
        self.assertIsNone(ca.advisor_clear_dt(self._arrival("completed"), DAY))
        self.assertIsNone(ca.advisor_clear_dt(self._arrival("cancelled"), DAY))

    def test_overnight_ambiguous_abstains(self):
        leg = _leg(2, 0, 30, trip="arrival", pickup="MCO Terminal B",
                   dropoff="Disney Contemporary",
                   flight=_flight(_dt(0, 5)))
        self.assertIsNone(ca.advisor_clear_dt(leg, DAY))
        # Confirmed night -> normal clock again.
        leg.overnight_confirmed_at = timezone.now()
        self.assertIsNotNone(ca.advisor_clear_dt(leg, DAY))

    def test_unknown_mode_refused(self):
        with self.assertRaises(ValueError):
            ca.advisor_clear_dt(self._arrival(), DAY, mode="hopeful")


# ════════════════════════════════════════════════════════════════════════════
# PRIME-DIRECTIVE NET (guard 13) — founder tight day => ZERO cards
# ════════════════════════════════════════════════════════════════════════════
class PrimeDirectiveNetTests(_PureBoardTestCase):
    def test_founder_built_tight_day_produces_zero_cards(self):
        # Driver 1 — the founder's classic MCO chain at +0 / +3 minutes:
        #   A: 2:00 arrival, clears 3:15 (45 dwell + 30 drive)
        #   B: 3:27 Disney pickup  -> 3:27 - (3:15 + 12 repo) = +0
        #   C: 3:54 Disney pickup  -> 3:54 - (3:39 + 12 repo) = +3
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 27, pickup="Disney Polynesian",
                 dropoff="Disney Boardwalk")
        c = _leg(103, 15, 54, pickup="Disney Grand Floridian",
                 dropoff="Disney Beach Club")
        # Driver 2 — another +0 pair.
        d = _leg(201, 10, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", driver_id=2)
        e = _leg(202, 11, 27, pickup="Disney Polynesian",
                 dropoff="Disney Boardwalk", driver_id=2)
        board = _board({1: [a, b, c], 2: [d, e]}, now=_dt(8, 0))
        self.assertEqual(ca.detect_disruptions(board), [])


# ════════════════════════════════════════════════════════════════════════════
# Overlap detection
# ════════════════════════════════════════════════════════════════════════════
class OverlapDetectionTests(_PureBoardTestCase):
    def _pair(self, next_mm=10, **next_kw):
        # A clears 15:15; a 15:10 next pickup is 17 min short (repo 12).
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, next_mm, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk", **next_kw)
        return a, b

    def test_planning_negative_pair_is_a_critical_card(self):
        a, b = self._pair()
        cards = ca.detect_disruptions(_board({1: [a, b]}, now=_dt(8, 0)))
        self.assertEqual(len(cards), 1)
        d = cards[0]
        self.assertEqual((d.kind, d.severity, d.id),
                         ("overlap", "critical", "overlap:101:102"))
        self.assertEqual(d.leg_ids, [101, 102])
        self.assertEqual(d.basis, ca.BASIS_CLOCK_ONLY)
        self.assertEqual(d.impact_dt, _dt(15, 10))
        self.assertIn("17 min short", d.headline)

    def test_recorded_early_pickup_reanchors_no_false_card(self):
        # Same impossible-on-paper pair — but the driver ALREADY picked up at
        # 2:05, so the dwell is spent and he clears 2:35: 23 real minutes of
        # slack. The card must vanish (reality beats the plan, on facts).
        a, b = self._pair(status="picked-up")
        board = _board({1: [a, b]}, now=_dt(14, 30),
                       picked={101: _dt(14, 5)})
        self.assertEqual(ca.detect_disruptions(board), [])

    def test_next_pickup_already_made_is_not_a_card(self):
        a, b = self._pair()
        board = _board({1: [a, b]}, now=_dt(15, 20),
                       picked={101: _dt(14, 40), 102: _dt(15, 12)})
        self.assertEqual(
            [d for d in ca.detect_disruptions(board) if d.kind == "overlap"],
            [])

    def test_fresh_on_time_gps_on_next_pickup_suppresses(self):
        a, b = self._pair(dispatch_risk_status="on_time",
                          dispatch_eta_minutes=6, dispatch_eta_target="pickup",
                          dispatch_eta_is_fresh=True)
        self.assertEqual(ca.detect_disruptions(_board({1: [a, b]},
                                                      now=_dt(8, 0))), [])

    def test_open_conflict_task_is_linked(self):
        a, b = self._pair()
        board = _board({1: [a, b]}, now=_dt(8, 0),
                       tasks={102: {"driver_conflict": 587}})
        self.assertEqual(ca.detect_disruptions(board)[0].task_id, 587)


# ════════════════════════════════════════════════════════════════════════════
# Late cascade — GPS/clock precedence (guards 2, 4)
# ════════════════════════════════════════════════════════════════════════════
class LateCascadeTests(_PureBoardTestCase):
    def _stalled(self, **kw):
        # Booked 1:00 PM, nothing recorded, status never progressed.
        return _leg(301, 13, 0, **kw)

    def test_clock_stalled_overdue_is_critical(self):
        cards = ca.detect_disruptions(
            _board({1: [self._stalled()]}, now=_dt(13, 20)))
        self.assertEqual(len(cards), 1)
        d = cards[0]
        self.assertEqual((d.kind, d.severity, d.basis),
                         ("late_cascade", "critical", ca.BASIS_CLOCK_ONLY))
        self.assertIn("20 min past the expected pickup", d.narrative)

    def test_fresh_on_time_gps_suppresses_the_clock(self):
        leg = self._stalled(dispatch_risk_status="on_time",
                            dispatch_eta_minutes=4,
                            dispatch_eta_target="pickup",
                            dispatch_eta_is_fresh=True)
        self.assertEqual(ca.detect_disruptions(_board({1: [leg]},
                                                      now=_dt(13, 20))), [])

    def test_stale_gps_yields_to_the_clock(self):
        leg = self._stalled(dispatch_risk_status="on_time",
                            dispatch_eta_minutes=4,
                            dispatch_eta_target="pickup",
                            dispatch_eta_is_fresh=False)   # stale snapshot
        cards = ca.detect_disruptions(_board({1: [leg]}, now=_dt(13, 20)))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].severity, "critical")

    def test_stale_parked_gps_is_never_a_disruption(self):
        # Two hours before pickup, GPS snapshot stale and parked — even an
        # at_risk *stale* band must not generate anything (guard 4).
        leg = _leg(301, 15, 0, dispatch_risk_status="at_risk",
                   dispatch_eta_minutes=90, dispatch_eta_target="pickup",
                   dispatch_eta_is_fresh=False, dispatch_is_moving=False)
        self.assertEqual(ca.detect_disruptions(_board({1: [leg]},
                                                      now=_dt(13, 0))), [])

    def test_overdue_stale_is_hygiene_only(self):
        cards = ca.detect_disruptions(
            _board({1: [self._stalled()]}, now=_dt(14, 0)))   # 60 min > 45
        self.assertEqual(len(cards), 1)
        d = cards[0]
        self.assertTrue(d.hygiene)
        self.assertTrue(d.abstain)
        self.assertEqual((d.kind, d.severity), ("late_cascade", "watch"))
        self.assertIn("chase the button", d.headline.lower())

    def test_overdue_but_moving_with_nothing_downstream_stays_quiet(self):
        leg = self._stalled(status="on-the-way")
        self.assertEqual(ca.detect_disruptions(_board({1: [leg]},
                                                      now=_dt(13, 20))), [])

    def test_fresh_gps_at_risk_cascades_down_the_chain(self):
        g = _leg(401, 14, 0, status="on-the-way",
                 dispatch_risk_status="at_risk", dispatch_eta_minutes=45,
                 dispatch_risk_reason="ETA 45 min exceeds time to pickup",
                 dispatch_eta_target="pickup",
                 dispatch_eta_target_time=timezone.make_aware(_dt(14, 0)),
                 dispatch_eta_is_fresh=True, dispatch_is_moving=True)
        h = _leg(402, 14, 30, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        cards = ca.detect_disruptions(_board({1: [g, h]}, now=_dt(13, 40)))
        self.assertEqual(len(cards), 1)
        d = cards[0]
        self.assertEqual((d.kind, d.severity, d.basis),
                         ("late_cascade", "critical", ca.BASIS_GPS_FRESH))
        self.assertEqual(d.leg_ids, [401, 402])          # the broken pickup rides along
        self.assertEqual(d.impact_dt, _dt(14, 30))
        # The sweep's own reason string is quoted verbatim.
        self.assertIn("ETA 45 min exceeds time to pickup", d.narrative)

    def test_fresh_gps_at_risk_without_downstream_break_is_warning(self):
        g = _leg(401, 14, 0, status="on-the-way",
                 dispatch_risk_status="at_risk", dispatch_eta_minutes=45,
                 dispatch_eta_target="pickup",
                 dispatch_eta_target_time=timezone.make_aware(_dt(14, 0)),
                 dispatch_eta_is_fresh=True)
        cards = ca.detect_disruptions(_board({1: [g]}, now=_dt(13, 40)))
        self.assertEqual([(d.kind, d.severity) for d in cards],
                         [("late_cascade", "warning")])


# ════════════════════════════════════════════════════════════════════════════
# Flight change — cause over symptom; the turn IN relaxes (never "late")
# ════════════════════════════════════════════════════════════════════════════
class FlightChangeTests(_PureBoardTestCase):
    def test_delayed_flight_cards_the_turn_out_not_a_late_driver(self):
        # Booked 2:00, flight now landing 3:00 (+60): the driver is NOT late
        # (no late_cascade — the plane hasn't landed), but the shifted clear
        # (4:15) breaks the 3:30 turn out. ONE card: the flight_change cause —
        # never an overlap symptom for the same pair.
        c = _leg(501, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", flight=_flight(_dt(15, 0)))
        f = _leg(502, 15, 30, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        cards = ca.detect_disruptions(_board({1: [c, f]}, now=_dt(14, 30)))
        self.assertEqual(len(cards), 1)
        d = cards[0]
        self.assertEqual((d.kind, d.severity, d.basis),
                         ("flight_change", "critical", ca.BASIS_FLIGHT))
        self.assertEqual(d.id, "flight_change:501")
        self.assertEqual(d.leg_ids, [501, 502])
        self.assertNotIn("overlap:501:502",
                         [x.id for x in cards])

    def test_turn_into_a_delayed_arrival_relaxes_and_nothing_is_carded(self):
        # On paper the 1:45 Disney job can't reposition to a 2:00 MCO arrival
        # (-27). But the flight lands 3:00, so the driver is truly due 3:10 —
        # the turn IN gained 70 minutes. Nothing is broken in either direction
        # and the arrival is the last job of the day, so the board stays
        # silent: a plane that moved is not, by itself, a card.
        p = _leg(511, 13, 45, pickup="Disney Contemporary",
                 dropoff="Disney Polynesian")
        c = _leg(512, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", flight=_flight(_dt(15, 0)))
        self.assertEqual(
            ca.detect_disruptions(_board({1: [p, c]}, now=_dt(8, 0))), [])

    def test_early_flight_is_never_a_card(self):
        """An early plane cannot tighten anything, so it can never break
        anything. _effective_pickup_dt refuses to pull a deadline earlier —
        the booked time is the guest's commitment — so the chain is byte
        identical to the plan and there is nothing to report."""
        c = _leg(521, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_flight(_dt(13, 30), scheduled=_dt(14, 0)))
        f = _leg(522, 16, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        self.assertEqual(
            ca.detect_disruptions(_board({1: [c, f]}, now=_dt(8, 0))), [])

    def test_an_early_plane_is_never_blamed_for_a_turn_it_cannot_have_broken(self):
        """The board is impossible on paper — the 2:00 arrival cannot clear in
        time for the 3:00 job — and the plane happens to be 30 min EARLY. An
        early plane moves neither the deadline nor the clear time, so this
        break predates it: the overlap detector owns it, and no flight card
        may claim the pair and re-label a planning fault as a flight problem."""
        c = _leg(581, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_flight(_dt(13, 30), scheduled=_dt(14, 0)))
        f = _leg(582, 15, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        kinds = [d.kind for d in
                 ca.detect_disruptions(_board({1: [c, f]}, now=_dt(8, 0)))]
        self.assertEqual(kinds.count("flight_change"), 0, kinds)
        self.assertEqual(kinds.count("overlap"), 1, kinds)

    def test_flight_running_on_schedule_is_silent_however_the_booking_reads(self):
        """PRODUCTION NOISE, 2026-08-05: "a flight is landing 17 minutes
        later", carded forever, with nothing having moved.

        best_arrival_local falls back to the published SCHEDULE, so a leg
        booked 17 min off its flight's timetable used to read as a permanent
        17-minute delay from the day it was booked. The plane here is running
        exactly on time; the offset is how the booking was written, and a
        booking is not a degradation."""
        c = _leg(541, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_flight(_dt(14, 17), scheduled=_dt(14, 17)))
        f = _leg(542, 18, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [c, f]}, now=_dt(8, 0))
        self.assertEqual(ca._flight_shift_min(c), 0)   # it has not moved at all
        self.assertEqual(ca.detect_disruptions(board), [])

    def test_a_flight_with_only_a_timetable_has_not_moved(self):
        """No estimate and no touchdown means the plane has reported nothing.
        Whatever best_arrival_local returns there is the timetable talking, and
        a timetable cannot have degraded."""
        c = _leg(543, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_FakeFlight(
                     ident="DL9", actual_gate_arrival_local=None,
                     estimated_gate_arrival_local=None,
                     actual_arrival_local=None, estimated_arrival_local=None,
                     scheduled_gate_arrival_local=_dt(15, 0),
                     scheduled_arrival_local=None))
        self.assertIsNone(ca._flight_shift_min(c))

    def test_moved_flight_that_breaks_nothing_is_silent(self):
        """The plane genuinely slipped 73 minutes — a big, true, useless fact.
        The next job is hours away, so nothing breaks and nothing is carded."""
        c = _leg(551, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_flight(_dt(15, 30), scheduled=_dt(14, 17)))
        f = _leg(552, 18, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [c, f]}, now=_dt(8, 0))
        self.assertEqual(ca._flight_shift_min(c), 73)   # it really did move
        self.assertEqual(ca.detect_disruptions(board), [])

    def test_moved_flight_that_thins_a_clean_turn_is_a_warning(self):
        """Reality DEGRADED — the other half of the prime directive. A clean
        63-minute turn is down to 3 because the plane slipped an hour. That is
        a consequence, so it cards; the founder never planned this one tight."""
        c = _leg(561, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_flight(_dt(15, 0), scheduled=_dt(14, 0)))
        f = _leg(562, 16, 30, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        d = _one_card(_board({1: [c, f]}, now=_dt(8, 0)), "flight_change")
        self.assertEqual(d.severity, "warning")
        self.assertEqual((d.details["slack_out"], d.details["slack_before"]),
                         (3, 63))

    def test_unacked_time_change_without_a_consequence_is_silent(self):
        """PRODUCTION NOISE, 2026-08-05: "a pickup time changed and nobody
        acknowledged it", with nothing behind it.

        The board already carries this fact on the leg row — a purple tint, a
        "⏰ was 12:30 PM" pill and the ✓ button that clears it. An advisor card
        repeating it with no consequence is a weaker duplicate of a control
        already on screen."""
        u = _leg(531, 13, 0, has_unacked_time_change=True,
                 pickup_time_was=dt_time(12, 30))
        self.assertEqual(
            ca.detect_disruptions(_board({1: [u]}, now=_dt(8, 0))), [])

    def test_unacked_time_change_that_wrecks_a_turn_still_cards(self):
        """The same flag WITH a consequence is exactly what the rail is for.
        Moved 12:00 -> 14:30, which eats a clean 156-minute turn down to 6."""
        u = _leg(531, 14, 30, has_unacked_time_change=True,
                 pickup_time_was=dt_time(12, 0),
                 pickup="Disney Contemporary", dropoff="Disney Polynesian")
        nxt = _leg(532, 15, 0, pickup="Disney Grand Floridian",
                   dropoff="Disney Boardwalk")
        d = _one_card(_board({1: [u, nxt]}, now=_dt(8, 0)), "flight_change")
        self.assertEqual(d.severity, "warning")
        self.assertTrue(d.details["unacked"])
        self.assertEqual((d.details["slack_out"], d.details["slack_before"]),
                         (6, 156))

    def test_affiliate_leg_raises_nothing(self):
        """Guard 7, narrowed 2026-08-05 on the owner's call: we do not monitor
        affiliate timing — they run their own chain — and with no chain math
        available there is nothing here a dispatcher could be told to do."""
        aff = _FakeDriver(name="Cheapo Limo", driver_type="affiliate")
        leg = _leg(571, 14, 0, trip="arrival", pickup="MCO Terminal B",
                   dropoff="Disney Contemporary",
                   flight=_flight(_dt(15, 30), scheduled=_dt(14, 0)),
                   driver_id=9, has_unacked_time_change=True,
                   pickup_time_was=dt_time(12, 0))
        board = _board({2: []}, now=_dt(8, 0), extra_legs=[leg],
                       drivers={9: aff})
        self.assertEqual(ca.detect_disruptions(board), [])

    def test_overnight_ambiguous_abstains_with_a_confirm_card(self):
        o = _leg(541, 0, 30, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", flight=_flight(_dt(0, 5)))
        cards = ca.detect_disruptions(_board({1: [o]}, now=_dt(0, 0)))
        self.assertEqual(len(cards), 1)
        d = cards[0]
        self.assertEqual((d.kind, d.severity), ("flight_change", "warning"))
        self.assertTrue(d.abstain)
        self.assertIn("Confirm which night", d.headline)


# ════════════════════════════════════════════════════════════════════════════
# Overnight tail (guard 3)
# ════════════════════════════════════════════════════════════════════════════
class OvernightTailTests(_PureBoardTestCase):
    def test_same_board_tail_pair_is_never_a_false_overlap(self):
        # Tonight's board shows last night's completed 00:15 tail AND a 23:30
        # job. Sorted by pickup_time the tail sorts first; nothing may pair
        # them into a fake negative turn.
        x = _leg(601, 0, 15, status="completed")
        y = _leg(602, 23, 30)
        self.assertEqual(ca.detect_disruptions(_board({1: [x, y]},
                                                      now=_dt(8, 0))), [])

    def test_genuine_tonight_to_tail_break_is_caught_across_midnight(self):
        # Last night's 11:50 PM MCO arrival clears 1:05 AM (absolute datetimes
        # under the PREVIOUS date); tonight's 00:15 tail pickup is 62 min
        # short. The one shared slack formula must catch it.
        prev_leg = _leg(610, 23, 50, trip="arrival", pickup="MCO Terminal B",
                        dropoff="Disney Contemporary", day=PREV)
        t = _leg(611, 0, 15, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board(
            {1: [t]}, now=_dt(23, 0, day=PREV),
            prev_tail={1: [(_make_sim_slot(prev_leg, PREV), None)]})
        cards = ca.detect_disruptions(board)
        self.assertEqual(len(cards), 1)
        d = cards[0]
        self.assertEqual((d.kind, d.severity, d.id),
                         ("overlap", "critical", "overlap:610:611"))
        self.assertTrue(d.details["cross_midnight"])
        self.assertIn("across midnight", d.narrative)


# ════════════════════════════════════════════════════════════════════════════
# Unassigned horizon
# ════════════════════════════════════════════════════════════════════════════
class UnassignedTests(_PureBoardTestCase):
    def _solo(self, hh, mm=0, now=None, **kw):
        leg = _leg(701, hh, mm, driver_id=None, **kw)
        return ca.detect_disruptions(
            _board({}, now=now or _dt(12, 0), extra_legs=[leg]))

    def test_inside_horizon_is_warning(self):
        cards = self._solo(13, 30)   # 90 min out
        self.assertEqual([(d.kind, d.severity) for d in cards],
                         [("unassigned", "warning")])

    def test_inside_half_horizon_is_critical(self):
        cards = self._solo(12, 45)   # 45 min out
        self.assertEqual(cards[0].severity, "critical")

    def test_beyond_horizon_is_quiet(self):
        self.assertEqual(self._solo(15, 30), [])   # 210 min out

    def test_recently_past_due_is_critical(self):
        cards = self._solo(11, 30)   # 30 min past
        self.assertEqual(cards[0].severity, "critical")
        self.assertIn("PAST pickup", cards[0].headline)

    def test_long_past_due_is_hygiene(self):
        cards = self._solo(11, 0)    # 60 min past > OVERDUE_STALE_MIN (45)
        d = cards[0]
        self.assertTrue(d.hygiene)
        self.assertEqual(d.severity, "watch")
        self.assertIn("Confirm coverage", d.headline)

    def test_hygiene_itself_ages_out(self):
        """The ladder has a last rung. Left at "permanent", these were half of
        the 11:46-at-5:40pm complaint — abstain cards cost no plan budget but
        they cost rail space in front of live work."""
        self.assertEqual(self._solo(10, 20), [])   # past ADVISOR_HYGIENE_TTL_MIN


# ════════════════════════════════════════════════════════════════════════════
# Overrun
# ════════════════════════════════════════════════════════════════════════════
class OverrunTests(_PureBoardTestCase):
    def _running(self, **kw):
        # Disney->Disney picked up 12:00, estimate ends 12:12.
        return _leg(801, 12, 0, status="picked-up", **kw)

    def test_running_long_with_slack_behind_is_a_warning(self):
        o = self._running()
        n = _leg(802, 13, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        cards = ca.detect_disruptions(
            _board({1: [o, n]}, now=_dt(12, 45), picked={801: _dt(12, 0)}))
        self.assertEqual([(d.kind, d.severity) for d in cards],
                         [("overrun", "warning")])
        self.assertEqual(cards[0].basis, ca.BASIS_RECORDED_PICKUP)

    def test_running_long_breaking_the_next_pickup_is_critical(self):
        o = self._running()
        n = _leg(802, 12, 50, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        cards = ca.detect_disruptions(
            _board({1: [o, n]}, now=_dt(12, 45), picked={801: _dt(12, 0)}))
        self.assertEqual(cards[0].severity, "critical")
        self.assertEqual(cards[0].leg_ids, [801, 802])

    def test_midtrip_gps_blowing_next_pickup_reads_the_sweep_not_a_recompute(self):
        o = self._running(dispatch_risk_status="at_risk",
                          dispatch_eta_target="next_pickup",
                          dispatch_eta_minutes=70,
                          dispatch_risk_reason="Mid-trip ETA exceeds slack",
                          dispatch_eta_is_fresh=True)
        n = _leg(802, 13, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        cards = ca.detect_disruptions(
            _board({1: [o, n]}, now=_dt(12, 5), picked={801: _dt(12, 0)}))
        self.assertEqual([(d.severity, d.basis) for d in cards],
                         [("critical", ca.BASIS_GPS_FRESH)])
        self.assertIn("Mid-trip ETA exceeds slack", cards[0].narrative)


# ════════════════════════════════════════════════════════════════════════════
# Ranking + serialization contract
# ════════════════════════════════════════════════════════════════════════════
class RankingAndSerializationTests(_PureBoardTestCase):
    def _mixed_board(self):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        u = _leg(701, 13, 30, driver_id=None)
        return _board({1: [a, b]}, now=_dt(12, 0), extra_legs=[u])

    def test_severity_then_impact_then_id_deterministic(self):
        cards = ca.detect_disruptions(self._mixed_board())
        self.assertEqual([d.id for d in cards],
                         ["overlap:101:102", "unassigned:701"])
        # Identical inputs -> identical output, twice over.
        again = ca.detect_disruptions(self._mixed_board())
        self.assertEqual([d.id for d in cards], [d.id for d in again])

    def test_detection_card_contract_shape(self):
        card = ca.serialize_disruption(
            ca.detect_disruptions(self._mixed_board())[0])
        self.assertEqual(
            set(card),
            {"id", "kind", "severity", "headline", "narrative", "impact_at",
             "leg_ids", "task_id", "basis"})
        self.assertEqual(card["impact_at"], "2026-05-01T15:10")
        self.assertEqual(card["leg_ids"], [101, 102])

    def test_kill_switch(self):
        from unittest.mock import patch
        with patch.object(ca, "ADVISOR_ENABLED", False):
            self.assertEqual(ca.detect_disruptions(self._mixed_board()), [])


# ════════════════════════════════════════════════════════════════════════════
# Fingerprint (guard 11) — DB tests
# ════════════════════════════════════════════════════════════════════════════
class FingerprintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from rates.models import Location, Rate, Route, Vehicle
        from reservations.models import Customer, Leg, Reservation
        from drivers.models import Driver

        vehicle = Vehicle.objects.create(vehicle_type="sedan", capacity=4,
                                         luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        route = Route.objects.create(origin=origin, destination=dest,
                                     inhouse_base_pay=Decimal("50.00"))
        customer = Customer.objects.create(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone_number="5551234567")
        rate = Rate.objects.create(vehicle=vehicle, route=route,
                                   oneway_price=Decimal("100.00"),
                                   round_trip_price=Decimal("180.00"))
        res = Reservation.objects.create(
            trip_type="one-way", customer=customer, vehicle=vehicle, rate=rate,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        cls.day = DAY
        cls.leg = Leg.objects.create(
            reservation=res, pickup_date=cls.day, pickup_time=dt_time(14, 0),
            pickup_location="MCO Terminal B",
            dropoff_location="Disney Contemporary", status="confirmed")
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user("fp-drv", password="x"),
            driver_type="inhouse")

    def test_stable_on_identical_state(self):
        # Pinned clock: the hash now carries a coarse time bucket, so identical
        # STATE is only identical when read at the same moment.
        now = timezone.make_aware(_dt(14, 2))
        self.assertEqual(ca.compute_board_fingerprint(self.day, now=now),
                         ca.compute_board_fingerprint(self.day, now=now))

    def test_the_clock_bucket_moves_the_hash_on_an_otherwise_dead_board(self):
        """Without this the whole expiry fix is invisible. On a quiet board —
        no taps, no roster edits — the three queries below never change, the
        endpoint answers "unchanged" every 60s, and the dead 11:46 card sits on
        screen at 5:40 because the rail never re-renders."""
        early = ca.compute_board_fingerprint(
            self.day, now=timezone.make_aware(_dt(14, 2)))
        later = ca.compute_board_fingerprint(
            self.day, now=timezone.make_aware(_dt(14, 44)))
        self.assertNotEqual(early, later)

    def test_the_bucket_is_coarse_enough_to_keep_the_short_circuit_useful(self):
        """It is a staleness sweep, not a per-second cache-buster: two reads
        inside the same bucket still short-circuit."""
        a = ca.compute_board_fingerprint(
            self.day, now=timezone.make_aware(_dt(14, 1)))
        b = ca.compute_board_fingerprint(
            self.day, now=timezone.make_aware(_dt(14, 3)))
        self.assertEqual(a, b)

    def test_budget_is_three_queries(self):
        with self.assertNumQueries(3):
            ca.compute_board_fingerprint(self.day)

    def test_leg_status_insert_bumps(self):
        from reservations.models import LegStatus
        before = ca.compute_board_fingerprint(self.day)
        LegStatus.objects.create(leg=self.leg, status="on-the-way")
        self.assertNotEqual(before, ca.compute_board_fingerprint(self.day))

    def test_roster_row_bumps(self):
        from drivers.models import DriverVehicleAssignment
        before = ca.compute_board_fingerprint(self.day)
        DriverVehicleAssignment.objects.create(driver=self.driver,
                                               date=self.day)
        self.assertNotEqual(before, ca.compute_board_fingerprint(self.day))

    def test_pickup_move_bumps(self):
        from reservations.models import Leg
        before = ca.compute_board_fingerprint(self.day)
        Leg.objects.filter(id=self.leg.id).update(pickup_time=dt_time(14, 30))
        self.assertNotEqual(before, ca.compute_board_fingerprint(self.day))


# ════════════════════════════════════════════════════════════════════════════
# STAGE B2 — candidate generation / validation / ranking / state contract
# ════════════════════════════════════════════════════════════════════════════
import json
from decimal import Decimal as _D
from unittest.mock import patch


def _res(vtype):
    """Reservation shim carrying the booked vehicle class (what the swap
    engine's vehicle-compat gate reads)."""
    return SimpleNamespace(vehicle=SimpleNamespace(vehicle_type=vtype),
                           customer=None, store_stop=False)


class _FakeDriver(SimpleNamespace):
    def __str__(self):
        return self.name


def _fake_farm_ctx(base=120):
    """Injectable farm-pricing context (quote/options), shaped like the real
    waterfall per-leg dicts. Gates run BEFORE pricing in the engine, so gate
    tests exercise the real gate code with this stub only ever pricing."""
    return SimpleNamespace(
        quote=lambda leg, day: {"status": "ok", "leg_id": leg.id,
                                "affiliate": "Oualid", "affiliate_id": 901,
                                "base": _D(base), "night": _D(0),
                                "total": _D(base)},
        options=lambda leg, day: ([{"driver_id": 901, "name": "Oualid",
                                    "base": _D(base), "night": _D(0),
                                    "total": _D(base)}], []))


def _one_card(board, kind=None):
    cards = ca.detect_disruptions(board)
    if kind:
        cards = [c for c in cards if c.kind == kind]
    assert len(cards) == 1, [c.id for c in cards]
    return cards[0]


class _SwapBoardMixin:
    """The swap-fixes-it fixture: D1 holds an impossible arrival→Disney pair;
    the only receiver (D2, SUV) is blocked by a towncar job that D3 (towncar)
    can absorb — a depth-1 swap chain is the clean fix; farming is the paid
    fallback."""

    def _swap_board(self, vip=None, farm_ctx="none"):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", reservation=_res("suv"))
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk", reservation=_res("suv"))
        c = _leg(201, 15, 5, pickup="Disney Polynesian",
                 dropoff="Disney Beach Club", driver_id=2,
                 reservation=_res("towncar"))
        return _board({1: [a, b], 2: [c], 3: []}, now=_dt(8, 0),
                      vtypes={1: "suv", 2: "suv", 3: "towncar"},
                      vip=vip, farm_ctx=farm_ctx)


class SwapChainPlanTests(_PureBoardTestCase, _SwapBoardMixin):
    def test_swap_fixes_it_and_farming_is_never_offered(self):
        board = self._swap_board(farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        import dispatching.swap_optimizer as so
        with patch.object(so, "find_swaps", wraps=so.find_swaps) as spy:
            plans = ca.generate_plans(board, d)
        # Explicit budgets — the literal defaults (5/5000/5000) would be
        # silently replaced by SchedulerSettings (swap_optimizer.py:286).
        kw = spy.call_args.kwargs
        self.assertEqual(kw["max_depth"], 3)
        self.assertEqual(kw["max_iterations"], 2500)
        self.assertLessEqual(kw["time_limit_ms"], 1200)
        self.assertIs(kw["driver_windows"], board.windows)
        self.assertIs(kw["sharer_partners"], board.sharer_partners)

        self.assertTrue(plans)
        top = plans[0]
        self.assertEqual((top.kind, top.tier), ("swap_chain", 2))
        # C (towncar) hands to D3; B lands on D2.
        self.assertEqual(
            [(m.leg_id, m.to_driver_id) for m in top.moves],
            [(201, 3), (102, 2)])
        # OWNER RULE: farming is a last resort, not a competitor. The work can
        # stay in-house here, so no affiliate is priced and no farm card is
        # offered at all — at any rank.
        self.assertFalse([p for p in plans if p.farm_out],
                         [p.title for p in plans])
        # Moving a 'confirmed' leg carries the status-reset warning (guard 6).
        self.assertTrue(any("re-accept" in r for r in top.risks))

    def test_vip_touching_swap_solution_is_rejected(self):
        board = self._swap_board(vip={201})
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        for p in plans:
            self.assertFalse(any(m.leg_id == 201 for m in p.moves),
                             f"{p.kind} displaces the VIP leg")
        self.assertTrue(any("VIP" in x
                            for x in d.details.get("plan_diagnostic", [])))

    def test_no_internal_solution_keeps_swap_diagnostic(self):
        # No D3, no vehicle escape: nobody in-house can absorb the target.
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        c = _leg(201, 15, 5, pickup="Disney Polynesian",
                 dropoff="Disney Beach Club", driver_id=2)
        board = _board({1: [a, b], 2: [c]}, now=_dt(8, 0))
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        self.assertFalse([p for p in plans if p.tier in (1, 2)])
        self.assertIn("No in-house driver can absorb it",
                      d.details.get("swap_diagnostic", ""))


class PlanValidationWiringTests(_PureBoardTestCase):
    def _demotion_board(self):
        j1 = _leg(201, 10, 0, pickup="Disney Contemporary",
                  dropoff="Disney Polynesian", driver_id=2)
        b = _leg(701, 10, 35, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk", driver_id=None)
        return _board({2: [j1]}, now=_dt(9, 30), extra_legs=[b])

    def test_tight_variant_is_demoted_with_worsened_pairs_named(self):
        # Seating the unassigned 10:35 behind the 10:00 job leaves 11 min —
        # legal, but a NEW ''→tight pair: the plan survives, penalized and
        # named (worsened_pairs → risks), never silently clean.
        board = self._demotion_board()
        d = _one_card(board, "unassigned")
        plans = ca.generate_plans(board, d)
        re = [p for p in plans if p.kind == "reassign"]
        self.assertTrue(re)
        p = re[0]
        self.assertEqual(p.validation.new_tight_count, 1)
        self.assertTrue(any("Creates a tight turn" in r and "11 min" in r
                            for r in p.risks), p.risks)
        self.assertIn("depends_tight_turn", p.risk_flags)
        # Ranking contract: a ''→tight-worsening in-house plan is DEMOTED to
        # tier 3 so dollars vs new risk compete on score with the farm tier.
        self.assertEqual(p.tier, 3)

    def test_fix_breaking_a_later_trip_is_hard_rejected(self):
        # Wiring: a candidate the whole-board validator hard-rejects is
        # DROPPED (its reason feeds the card diagnostic), never surfaced.
        from dispatching.board_validation import BoardValidation
        board = self._demotion_board()
        d = _one_card(board, "unassigned")
        with patch("dispatching.board_validation.validate_post_move_board",
                   return_value=BoardValidation(
                       ok=False,
                       reason="turn 201->701 on driver 2 would go clean -> "
                              "critical (-4 min slack)")):
            plans = ca.generate_plans(board, d)
        self.assertFalse([p for p in plans if p.moves])
        self.assertTrue(any("critical" in x
                            for x in d.details.get("plan_diagnostic", [])))


class MatchFlightPlanTests(_PureBoardTestCase):
    def test_broken_turn_gets_the_combined_match_and_cover_plan(self):
        c = _leg(501, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", flight=_flight(_dt(15, 0)))
        f = _leg(502, 15, 30, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [c, f], 2: []}, now=_dt(14, 30))
        d = _one_card(board, "flight_change")
        plans = ca.generate_plans(board, d)
        self.assertEqual(plans[0].kind, "match_flight")
        self.assertEqual(plans[0].tier, 1)
        # Retime computed the way the Match-flight button computes it
        # (controlling best arrival .time()) + the broken 3:30 covered.
        payload = ca._serialize_plan(board, d, plans[0], 1)
        self.assertEqual(payload["apply"]["actions"][0]["op"], "retime")
        self.assertEqual(payload["apply"]["actions"][0]["new_pickup_time"],
                         "15:00")
        self.assertEqual(payload["apply"]["actions"][1],
                         {"op": "reassign", "leg_id": 502, "to_driver_id": 2})
        self.assertEqual(payload["apply"]["expected"],
                         {"501": 1, "502": 1})       # from-driver staleness map
        self.assertEqual(payload["apply"]["expected_times"], {"501": "14:00"})
        # The plain cover-only variant still comes from the tier-2 ladder.
        self.assertTrue(any(p.kind == "reassign" and p.tier == 2
                            for p in plans))

    def test_tightened_but_legal_turn_gets_monitor_then_match(self):
        c = _leg(501, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", flight=_flight(_dt(14, 20)))
        f = _leg(502, 16, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [c, f]}, now=_dt(8, 0))
        d = _one_card(board, "flight_change")
        self.assertEqual(d.severity, "warning")
        plans = ca.generate_plans(board, d)
        self.assertEqual([p.kind for p in plans][:2],
                         ["monitor", "match_flight"])
        payload = ca._serialize_plan(board, d, plans[1], 2)
        self.assertEqual(payload["apply"]["expected_times"], {"501": "14:00"})
        self.assertEqual(payload["apply"]["actions"][0]["new_pickup_time"],
                         "14:20")


class TakebackTests(_PureBoardTestCase):
    def test_takeback_pulls_an_affiliate_leg_back_in_house(self):
        """The takeback tier itself, exercised directly.

        No detector raises an affiliate card any more (see
        FlightChangeTests.test_affiliate_leg_raises_nothing), so this capability
        is currently unreachable through generate_plans. It is kept and kept
        covered deliberately: it is the correct recovery the moment any card
        touches affiliate-held work again, and an untested tier rots."""
        aff = _FakeDriver(name="Cheapo Limo", driver_type="affiliate")
        leg = _leg(501, 14, 0, trip="arrival", pickup="MCO Terminal B",
                   dropoff="Disney Contemporary",
                   flight=_flight(_dt(15, 0), scheduled=_dt(14, 0)),
                   driver_id=9)
        board = _board({2: []}, now=_dt(8, 0), extra_legs=[leg],
                       drivers={9: aff})
        d = ca.Disruption(
            id="flight_change:501", kind="flight_change", severity="critical",
            headline="x", narrative="x", basis=ca.BASIS_FLIGHT,
            leg_ids=[501], anchor_leg_id=501, driver_id=9,
            impact_dt=_dt(14, 0), details={"affiliate": True})
        tb = ca._takeback_plans(board, d, [])
        self.assertEqual(len(tb), 1)
        self.assertEqual([(m.leg_id, m.to_driver_id) for m in tb[0].moves],
                         [(501, 2)])
        self.assertTrue(any("Call Cheapo Limo first" in r
                            for r in tb[0].risks))
        # The serialized apply payload carries the confirm_pullback opt-in the
        # reused farmout hard rule demands — without it every Apply click
        # would 400 with no UI affordance to supply the flag.
        payload = ca._serialize_plan(board, d, tb[0], 1)
        self.assertIs(payload["apply"]["confirm_pullback"], True)


# ════════════════════════════════════════════════════════════════════════════
# Card expiry — a card a dispatcher cannot act on is not a quieter card, it is
# a wrong one. PRODUCTION, 2026-08-05: an 11:46 AM pickup still on the rail at
# 5:40 PM, burying the cards that could still be acted on.
# ════════════════════════════════════════════════════════════════════════════
class CardExpiryTests(_PureBoardTestCase):
    """Every shape gets the same two pins: ALIVE while it can still be changed,
    GONE once it cannot. The expiry key is deliberately NOT impact_dt — the
    late-driver and overrun shapes are BORN with their impact moment in the
    past, and gating on it would delete the best cards on the rail."""

    def _flight_board(self, now):
        a = _leg(501, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_flight(_dt(15, 0), scheduled=_dt(14, 0)))
        b = _leg(502, 15, 30, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        return _board({1: [a, b]}, now=now)

    def test_flight_card_dies_with_the_pickup_it_was_protecting(self):
        live = ca.detect_disruptions(self._flight_board(_dt(14, 30)))
        self.assertEqual([d.kind for d in live], ["flight_change"])
        self.assertEqual(live[0].expires_at, _dt(16, 15))   # 15:30 + 45
        self.assertEqual(
            [d for d in ca.detect_disruptions(self._flight_board(_dt(17, 40)))
             if d.kind == "flight_change"], [])

    def test_overdue_driver_is_urgent_precisely_because_impact_has_passed(self):
        # 13:00 pickup, nobody moving, seen at 13:20. impact_dt is 20 minutes
        # in the PAST and this is the most actionable card on the board.
        lc = _leg(701, 13, 0, pickup="Disney Contemporary",
                  dropoff="Disney Polynesian")
        d = _one_card(_board({1: [lc]}, now=_dt(13, 20)), "late_cascade")
        self.assertEqual(d.severity, "critical")
        self.assertLess(d.impact_dt, _dt(13, 20))       # born in the past
        self.assertGreater(d.expires_at, _dt(13, 20))   # and still live

    def test_the_late_driver_ladder_runs_live_then_hygiene_then_gone(self):
        def at(now):
            lc = _leg(701, 13, 0, pickup="Disney Contemporary",
                      dropoff="Disney Polynesian")
            return ca.detect_disruptions(_board({1: [lc]}, now=now))
        self.assertEqual([(d.severity, d.hygiene) for d in at(_dt(13, 20))],
                         [("critical", False)])
        self.assertEqual([(d.severity, d.hygiene) for d in at(_dt(13, 50))],
                         [("watch", True)])       # past OVERDUE_STALE_MIN
        self.assertEqual(at(_dt(15, 30)), [])     # past the hygiene TTL

    def test_the_ladder_has_no_gap_between_its_rungs(self):
        """A window where NEITHER rung exists is not a quieter rail, it is a
        blind spot. The live rung's deadline and the hygiene rung's birth are
        measured from DIFFERENT anchors — _effective_pickup_dt vs
        pickup_expected_dt — so the handover has to be pinned minute by minute
        rather than sampled either side of it."""
        for minute in range(40, 56):
            lc = _leg(701, 13, 0, pickup="Disney Contemporary",
                      dropoff="Disney Polynesian")
            cards = ca.detect_disruptions(
                _board({1: [lc]}, now=_dt(13, minute)))
            self.assertEqual(len(cards), 1, f"13:{minute:02d} -> {cards}")

    def test_an_airport_pickup_hands_over_on_the_airport_clock(self):
        """The 35-minute version of the same trap. On a flight-tracked arrival
        the driver is DUE at gate + 10 but the overdue clock only starts at
        gate + 45 (time to clear the airport), so a live card expiring on the
        first anchor died 35 minutes before its replacement was born — a
        36-minute hole on the trip type this company runs most."""
        for minute in (48, 55, 59):
            arr = _leg(711, 14, 0, trip="arrival", pickup="MCO Terminal B",
                       dropoff="Disney Contemporary",
                       flight=_flight(_dt(14, 0), scheduled=_dt(14, 0)))
            cards = ca.detect_disruptions(
                _board({1: [arr]}, now=_dt(14, minute)))
            self.assertEqual([d.kind for d in cards], ["late_cascade"],
                             f"14:{minute} -> {cards}")
        for hh, mm in ((15, 10), (15, 30), (16, 0)):
            arr = _leg(711, 14, 0, trip="arrival", pickup="MCO Terminal B",
                       dropoff="Disney Contemporary",
                       flight=_flight(_dt(14, 0), scheduled=_dt(14, 0)))
            cards = ca.detect_disruptions(
                _board({1: [arr]}, now=_dt(hh, mm)))
            self.assertEqual([d.kind for d in cards], ["late_cascade"],
                             f"{hh}:{mm} -> {cards}")

    def test_a_job_nobody_closed_out_stops_carding(self):
        """The worst of the stale shapes: "still running 309 min past its
        estimate" at 5:40 PM for an 11:46 AM job whose driver never tapped
        done. It was NOT abstain, so it also ate one of the six plan slots."""
        def at(now):
            o = _leg(801, 11, 46, status="picked-up",
                     pickup="Disney Contemporary", dropoff="Disney Polynesian")
            return ca.detect_disruptions(
                _board({1: [o]}, now=now, picked={801: _dt(11, 46)}))
        self.assertEqual([d.kind for d in at(_dt(12, 40))], ["overrun"])
        self.assertEqual(at(_dt(17, 40)), [])

    def test_uncovered_pickup_survives_its_moment_then_ages_out(self):
        """A guest may be standing on a curb, so this one deliberately outlives
        its pickup time — then becomes a record to fix, then goes quiet."""
        def at(now):
            u = _leg(901, 11, 46, driver_id=None, pickup="Disney Contemporary",
                     dropoff="Disney Polynesian")
            return ca.detect_disruptions(
                _board({1: []}, now=now, extra_legs=[u]))
        self.assertEqual([(d.severity, d.hygiene) for d in at(_dt(12, 0))],
                         [("critical", False)])   # 14 min past, still urgent
        self.assertEqual([(d.severity, d.hygiene) for d in at(_dt(12, 40))],
                         [("watch", True)])
        self.assertEqual(at(_dt(13, 30)), [])

    def test_every_card_leaves_the_detector_with_a_deadline(self):
        """The guarantee _add exists to make: no future detector can leak a
        card that lives until midnight by forgetting to set one."""
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary",
                 flight=_flight(_dt(15, 0), scheduled=_dt(14, 0)))
        b = _leg(102, 15, 30, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        u = _leg(103, 9, 30, driver_id=None)
        o = _leg(104, 8, 0, status="picked-up", driver_id=2)
        board = _board({1: [a, b], 2: [o]}, now=_dt(8, 45), extra_legs=[u],
                       picked={104: _dt(8, 0)})
        cards = ca.detect_disruptions(board)
        self.assertTrue(cards)
        for d in cards:
            self.assertIsNotNone(d.expires_at, d.id)
            self.assertGreater(d.expires_at, board.now_local, d.id)


# ════════════════════════════════════════════════════════════════════════════
# Guard 6b — no dispatching into the past, and no receiver who cannot get
# there. PRODUCTION, 2026-08-05: at 5:57 PM a "Shuffle 2 jobs" plan proposed
# handing a 4:00 PM pickup to another driver.
# ════════════════════════════════════════════════════════════════════════════
class PastPickupPlanTests(_PureBoardTestCase):
    def test_an_assigned_leg_whose_moment_passed_can_no_longer_be_moved(self):
        # Status is not a proxy for time: nobody tapped "picked up", so the
        # 4:00 leg is still 'confirmed' at 5:57 and every tier used to treat
        # it as freely movable.
        stale = _leg(401, 16, 0, driver_id=1, status="confirmed")
        board = _board({1: [stale]}, now=_dt(17, 57))
        self.assertFalse(ca._movable(board, stale))

    def test_the_same_leg_was_movable_before_its_moment(self):
        stale = _leg(401, 16, 0, driver_id=1, status="confirmed")
        self.assertTrue(ca._movable(_board({1: [stale]}, now=_dt(15, 30)),
                                    stale))

    def test_a_delayed_plane_keeps_the_job_movable_past_its_booked_time(self):
        """Reality moved this job, it did not expire: booked 4:00, plane now
        landing 6:15, so at 5:57 it is still very much re-homeable."""
        late = _leg(402, 16, 0, trip="arrival", pickup="MCO Terminal B",
                    dropoff="Disney Contemporary", driver_id=1,
                    flight=_flight(_dt(18, 15), scheduled=_dt(16, 0)))
        self.assertTrue(ca._movable(_board({1: [late]}, now=_dt(17, 57)), late))

    def test_an_unassigned_past_due_leg_can_still_be_covered(self):
        """The one late move that is exactly right — nobody is at that curb
        yet, and a guest may be waiting. Freezing this would break the most
        urgent card the advisor raises."""
        orphan = _leg(403, 16, 0, driver_id=None)
        board = _board({1: []}, now=_dt(16, 20), extra_legs=[orphan])
        self.assertTrue(ca._movable(board, orphan))

    def test_a_receiver_who_cannot_get_there_is_not_a_candidate(self):
        """check_feasibility has no clock — it answers "does this fit between
        his other jobs", and would hand back a healthy buffer for a driver who
        is mid-job across the county."""
        busy = _leg(411, 17, 30, driver_id=2, pickup="MCO Terminal B",
                    dropoff="Disney Contemporary", status="picked-up")
        target = _leg(412, 18, 0, driver_id=None,
                      pickup="Disney Grand Floridian",
                      dropoff="Disney Boardwalk")
        board = _board({2: [busy]}, now=_dt(17, 55), extra_legs=[target],
                       picked={411: _dt(17, 30)})
        self.assertEqual(ca._reach_dt(board, 2, target), _dt(18, 12))
        self.assertEqual(ca._receiver_candidates(board, target), [])

    def test_a_receiver_who_can_get_there_is_still_offered(self):
        """The guard must not turn into "never move anything late". The same
        driver with a shorter job in front of him clears in time and stays a
        candidate, tight buffer and all."""
        busy = _leg(413, 17, 30, driver_id=2, pickup="Disney Contemporary",
                    dropoff="Disney Polynesian", status="picked-up")
        target = _leg(414, 18, 0, driver_id=None,
                      pickup="Disney Grand Floridian",
                      dropoff="Disney Boardwalk")
        board = _board({2: [busy]}, now=_dt(17, 55), extra_legs=[target],
                       picked={413: _dt(17, 30)})
        self.assertEqual([did for did, _ in
                          ca._receiver_candidates(board, target)], [2])

    def test_a_past_due_pickup_still_gets_every_receiver_offered(self):
        """THE CARVE-OUT, ALL THE WAY DOWN. _reach_dt is floored at NOW, so for
        a pickup whose moment has gone by EVERY real driver scores
        reach >= now > due. Testing that would reject all of them and quietly
        delete the recovery from the most urgent card the advisor raises — a
        guest possibly standing at a curb, a card that says "cover this" and a
        plan list that says nothing. _movable keeps the leg placeable; the
        receiver filter must not take that back one layer down."""
        done = _leg(431, 15, 30, driver_id=2, pickup="Disney Contemporary",
                    dropoff="Disney Polynesian")
        orphan = _leg(432, 16, 0, driver_id=None,
                      pickup="Disney Grand Floridian",
                      dropoff="Disney Boardwalk")
        board = _board({2: [done]}, now=_dt(16, 20), extra_legs=[orphan])
        self.assertTrue(ca._movable(board, orphan))
        self.assertEqual([did for did, _ in
                          ca._receiver_candidates(board, orphan)], [2])

    def test_an_uncovered_past_due_pickup_still_comes_with_a_plan(self):
        """The same guarantee end to end: the card AND something to do about
        it. A card that names an uncovered guest and offers nothing is the
        failure this guard was supposed to prevent, not cause."""
        done = _leg(441, 15, 30, driver_id=2, pickup="Disney Contemporary",
                    dropoff="Disney Polynesian")
        orphan = _leg(442, 16, 0, driver_id=None,
                      pickup="Disney Grand Floridian",
                      dropoff="Disney Boardwalk")
        board = _board({2: [done]}, now=_dt(16, 20), extra_legs=[orphan])
        d = _one_card(board, "unassigned")
        self.assertEqual(d.severity, "critical")
        plans = ca.generate_plans(board, d)
        self.assertTrue([p for p in plans if p.moves], d.details)

    def test_a_break_the_engine_proved_stays_recoverable_past_its_moment(self):
        """The freeze protects a driver who might be AT the curb. When the
        engine has just finished proving he is somewhere else — his current job
        is running long, which is the entire card — freezing the pickup he is
        about to miss hands the dispatcher a problem and no options at the one
        moment they most need one."""
        long_job = _leg(451, 15, 0, status="picked-up", driver_id=1,
                        pickup="MCO Terminal B", dropoff="Disney Contemporary")
        breaking = _leg(452, 16, 0, driver_id=1,
                        pickup="Disney Grand Floridian",
                        dropoff="Disney Boardwalk")
        free = _leg(453, 15, 0, driver_id=2, pickup="Disney Polynesian",
                    dropoff="Disney Grand Floridian")
        board = _board({1: [long_job, breaking], 2: [free]}, now=_dt(16, 10),
                       picked={451: _dt(15, 0)})
        d = _one_card(board, "overrun")
        self.assertTrue(d.details["breaks"])
        # Frozen on its own, recoverable as the break the card is about.
        self.assertFalse(ca._movable(board, breaking))
        self.assertEqual([l.id for l in ca._recovery_targets(board, d)], [452])

    def test_a_driver_who_cleared_long_ago_has_no_knowable_position(self):
        """Not the same as reachable. He finished at 2pm; at 5:57 he could be
        anywhere, and arithmetic off a four-hour-old dropoff is the teleport
        assumption wearing a lab coat."""
        done = _leg(421, 13, 0, driver_id=2, pickup="Disney Contemporary",
                    dropoff="Disney Polynesian")
        target = _leg(422, 18, 0, driver_id=None,
                      pickup="Disney Grand Floridian",
                      dropoff="Disney Boardwalk")
        board = _board({2: [done]}, now=_dt(17, 57), extra_legs=[target])
        self.assertIsNone(ca._reach_dt(board, 2, target))


class FarmTierTests(_PureBoardTestCase):
    def _pair_board(self, extra=None, **kw):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        assignment = {1: [a, b]}
        assignment.update(extra or {})
        return _board(assignment, now=_dt(8, 0), **kw)

    def test_farming_is_a_last_resort_not_an_alternative(self):
        """OWNER RULE: an affiliate is only an option once the work cannot be
        kept in-house at all. With a free in-house receiver on the board, the
        farm tiers must not run — no pricing, no card, no option — even though
        the direct reassign manufactures a tight turn."""
        board = self._pair_board(extra={2: []}, farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        self.assertTrue([p for p in plans if p.moves and not p.farm_out])
        self.assertFalse([p for p in plans if p.farm_out],
                         [p.title for p in plans])

    def test_farming_is_offered_once_in_house_is_exhausted(self):
        """The same board with nobody free: farming is exactly what should be
        offered, and it is."""
        board = self._pair_board(farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        self.assertFalse([p for p in plans if p.moves and not p.farm_out])
        self.assertTrue([p for p in plans if p.farm_out],
                        [p.title for p in plans])

    def test_a_farm_plan_can_never_outrank_an_in_house_one(self):
        """Belt and braces on the ordering itself: even if both ever coexist,
        no score may put an affiliate above keeping the work ourselves."""
        board = self._pair_board(extra={2: []}, farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        farm = ca._direct_farm_plan(board, d, ca._recovery_targets(board, d),
                                    [], [])
        if farm is None:
            self.skipTest("fixture priced no farm plan")
        farm.score = 10 ** 6          # an absurdly attractive farm
        merged = plans + [farm]
        merged.sort(key=lambda p: (bool(p.farm_out), p.tier, -p.score, p.kind,
                                   p.target_leg_id, ()))
        self.assertFalse(merged[0].farm_out)

    def test_a_retime_that_breaks_the_turn_out_files_ONE_card(self):
        """CAUSE OVER SYMPTOM. A flight card already reporting that the turn out
        of its leg is broken owns that pair — the overlap detector must not file
        a second card for it. This held for a delayed plane but not for an
        unacknowledged pickup-time change, which produced two cards with the
        same headline, the same legs and the same single fix."""
        from datetime import time as _t
        a = _leg(601, 11, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Beach Club", driver_id=1,
                 has_unacked_time_change=True, pickup_time_was=_t(10, 30),
                 pickup_time_changed_at=_dt(8, 0))
        b = _leg(602, 12, 30, pickup="Disney Grand Floridian",
                 dropoff="MCO Terminal A", driver_id=1)
        board = _board({1: [a, b], 2: []}, now=_dt(9, 0))
        kinds = [d.kind for d in ca.detect_disruptions(board)]
        self.assertEqual(kinds.count("flight_change"), 1, kinds)
        self.assertEqual(kinds.count("overlap"), 0, kinds)

    def test_vip_is_never_farmed(self):
        board = self._pair_board(vip={102}, farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        for p in plans:
            self.assertFalse(any(m.op == "farm_out" and m.leg_id == 102
                                 for m in p.moves))
        self.assertTrue(any("VIP — never farmed" in ph
                            for ph in d.details.get("farm_protected", [])))

    def test_pending_refund_warned_on_reassign(self):
        board = self._pair_board(extra={2: []}, refunds={102},
                                 farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        warned = [p for p in plans if p.kind == "reassign"
                  and any(m.leg_id == 102 for m in p.moves)]
        self.assertTrue(warned)
        self.assertIn("Warning: This reservation has a pending refund "
                      "request.", warned[0].risks)

    def test_pending_refund_is_never_farmed_when_farming_is_reached(self):
        """The gate itself — on a board with NO in-house receiver, so the farm
        tier actually runs."""
        board = self._pair_board(refunds={102}, farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        for p in plans:
            self.assertFalse(any(m.op == "farm_out" and m.leg_id == 102
                                 for m in p.moves))
        self.assertTrue(any("refund in flight" in ph
                            for ph in d.details.get("farm_protected", [])))

    def test_evict_to_farm_farms_the_arrival(self):
        # D2's blocking job is an ARRIVAL (the farm currency); his morning
        # DEPARTURE is untouched. The evict plan farms the arrival and seats
        # the broken leg on D2.
        ar = _leg(201, 15, 0, trip="arrival", pickup="MCO Terminal B",
                  dropoff="Disney Polynesian", driver_id=2)
        dp = _leg(202, 10, 0, pickup="Disney Contemporary",
                  dropoff="MCO Terminal A", driver_id=2)
        board = self._pair_board(extra={2: [ar, dp]},
                                 farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        ev = [p for p in plans if p.kind == "evict_and_farm"]
        self.assertTrue(ev)
        self.assertEqual(
            [(m.leg_id, m.op) for m in ev[0].moves],
            [(201, "farm_out"), (102, "reassign")])
        self.assertTrue(any("farm currency" in w for w in ev[0].why))
        for p in plans:
            self.assertFalse(any(m.op == "farm_out" and m.leg_id == 202
                                 for m in p.moves),
                             "a departure was farmed")

    def test_departure_is_never_farmed_even_when_it_is_the_only_evictee(self):
        dp = _leg(202, 15, 0, pickup="Disney Contemporary",
                  dropoff="MCO Terminal A", driver_id=2)
        board = self._pair_board(extra={2: [dp]}, farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        for p in plans:
            self.assertFalse(any(m.op == "farm_out" and m.leg_id == 202
                                 for m in p.moves))
        self.assertFalse([p for p in plans if p.kind == "evict_and_farm"])
        self.assertTrue(any("true departure — stays in-house" in ph
                            for ph in d.details.get("farm_protected", [])))

    def test_farm_card_lines_and_apply_has_ids_never_dollars(self):
        board = self._pair_board(farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        farm = [p for p in plans if p.kind == "farm_out"]
        self.assertTrue(farm)
        p = farm[0]
        self.assertIn("gps_blind_affiliate", p.risk_flags)
        # Farm cards END with the call-affiliate-to-confirm line (SOP).
        self.assertIn("call Oualid to confirm", p.risks[-1])
        payload = ca._serialize_plan(board, d, p, 1)
        self.assertEqual(payload["price_impact"], 120.0)   # display only
        apply_json = json.dumps(payload["apply"])
        self.assertNotIn("$", apply_json)
        self.assertNotIn("price", apply_json)
        farm_action = payload["apply"]["actions"][0]
        self.assertEqual((farm_action["op"], farm_action["to_driver_id"]),
                         ("farm_out", 901))


class StatusSafetyTests(_PureBoardTestCase):
    def test_picked_up_and_on_location_legs_never_move(self):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", status="picked-up")
        b = _leg(102, 15, 40, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk", status="on-location")
        board = _board({1: [a, b]}, now=_dt(15, 10),
                       picked={101: _dt(15, 5)}, farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "overlap")
        self.assertEqual(d.severity, "critical")
        self.assertEqual(ca.generate_plans(board, d), [])

    def test_moving_an_on_the_way_leg_warns_status_reset(self):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk", status="on-the-way")
        board = _board({1: [a, b], 2: []}, now=_dt(8, 0))
        d = _one_card(board, "overlap")
        plans = [p for p in ca.generate_plans(board, d)
                 if any(m.leg_id == 102 for m in p.moves)]
        self.assertTrue(plans)
        self.assertTrue(any("already on-the-way" in r and "re-accept" in r
                            for r in plans[0].risks))


class MoveSanctionTests(_PureBoardTestCase):
    def test_clock_only_critical_without_downstream_break_never_moves_work(self):
        # Guard 5: a booked 1:00 PM, status still confirmed, no GPS at all,
        # 20 min past the grace — clock-critical, but with ZERO downstream
        # negative slack the only evidence is an unpressed button. The card
        # stands; reassign/swap/farm plans must NOT offer to strip the leg.
        a = _leg(101, 13, 0, pickup="Disney Contemporary",
                 dropoff="Disney Polynesian")
        b = _leg(102, 18, 0, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [a, b], 2: []}, now=_dt(13, 20),
                       farm_ctx=_fake_farm_ctx())
        d = _one_card(board, "late_cascade")
        self.assertEqual(d.severity, "critical")
        self.assertEqual(d.basis, ca.BASIS_CLOCK_ONLY)
        self.assertFalse(d.details.get("breaks"))
        self.assertFalse(ca._may_move_work(d))
        self.assertFalse([p for p in ca.generate_plans(board, d) if p.moves])

    def test_clock_only_critical_with_downstream_break_may_move_work(self):
        # The same clock-only anchor WITH a concrete negative-slack pickup
        # behind it is a hard break — guard 5 sanctions moving the broken work.
        a = _leg(101, 13, 0, pickup="Disney Contemporary",
                 dropoff="Disney Polynesian")
        b = _leg(102, 13, 55, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [a, b], 2: []}, now=_dt(13, 40))
        d = _one_card(board, "late_cascade")
        self.assertEqual(d.severity, "critical")
        self.assertTrue(d.details.get("breaks"))
        self.assertTrue(ca._may_move_work(d))


class PlanningClockTests(_PureBoardTestCase):
    def test_late_recorded_pickup_blocks_the_receiver(self):
        # Guard 1, planning half: D2's 1:00 PM job was picked up at 3:00 PM
        # (recorded tap — the guest ran late). The static clock says D2
        # cleared around 2:00 and looks free for the 3:10 job; the planning
        # clock (max(static, actual)) knows he is still mid-job. The advisor
        # must not offer him.
        target = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                      dropoff="Disney Boardwalk", driver_id=None)
        r = _leg(201, 13, 0, pickup="Disney Contemporary",
                 dropoff="Disney Polynesian", driver_id=2, status="picked-up")

        clean = _board({2: [r]}, now=_dt(15, 5), extra_legs=[target])
        self.assertTrue(ca._receiver_candidates(clean, target),
                        "static clock should admit D2 — fixture broken")

        late = _board({2: [r]}, now=_dt(15, 5), extra_legs=[target],
                      picked={201: _dt(15, 0)})
        self.assertEqual(ca._receiver_candidates(late, target), [])

    def test_early_recorded_pickup_never_makes_planning_optimistic(self):
        # max(static, actual): an EARLY tap never relaxes the planning clock.
        r = _leg(201, 13, 0, pickup="Disney Contemporary",
                 dropoff="Disney Polynesian", driver_id=2, status="picked-up")
        board = _board({2: [r]}, now=_dt(13, 0), picked={201: _dt(12, 30)})
        scheds = ca.planning_clock_schedules(
            board.schedules, board.legs_by_id, board.picked_up_by_leg, DAY)
        slot = scheds[2].slots[0]
        self.assertEqual(slot.chain_clear_dt,
                         chain_clear_dt(r, DAY))   # static kept, not pulled in


class SharedVehicleGuardTests(_PureBoardTestCase):
    def test_shared_car_partner_blocks_the_receiver(self):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        partner_job = _leg(401, 15, 5, pickup="Disney Polynesian",
                           dropoff="Disney Beach Club", driver_id=4)
        board = _board({1: [a, b], 2: [], 4: [partner_job]}, now=_dt(8, 0),
                       sharers={2: {4}, 4: {2}})
        d = _one_card(board, "overlap")
        plans = ca.generate_plans(board, d)
        for p in plans:
            self.assertFalse(any(m.to_driver_id == 2 for m in p.moves),
                             "placed onto a share-blocked driver")
        self.assertIn("shares the physical car",
                      d.details.get("swap_diagnostic", ""))


class StubWindowAttributionTests(_PureBoardTestCase):
    def test_stub_window_receiver_is_attributed_honestly(self):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        stub_win = {"start": 6, "end": 23, "max_hours": None,
                    "flexible": False}
        board = _board({1: [a, b], 2: []}, now=_dt(8, 0),
                       windows={2: stub_win}, window_sources={2: "stub"})
        d = _one_card(board, "overlap")
        plans = [p for p in ca.generate_plans(board, d)
                 if any(m.to_driver_id == 2 for m in p.moves)]
        self.assertTrue(plans)
        self.assertIn("stub_window", plans[0].risk_flags)
        self.assertTrue(any("observed-history window (provisional)" in r
                            for r in plans[0].risks))


class AdvisorStateContractTests(_PureBoardTestCase):
    def _many_unassigned(self, n=8):
        legs = [_leg(700 + i, 13, 5 * i, driver_id=None) for i in range(n)]
        return _board({}, now=_dt(12, 0), extra_legs=legs)

    def test_budget_truncation_analyzes_at_most_six(self):
        state = ca._advisor_state(self._many_unassigned(8), "fp")
        self.assertTrue(state["truncated"])
        analyzed = [c for c in state["disruptions"] if not c["detected_only"]]
        deferred = [c for c in state["disruptions"] if c["detected_only"]]
        self.assertEqual(len(analyzed), 6)
        self.assertEqual(len(deferred), 2)
        self.assertEqual(state["fingerprint"], "fp")

    def test_exhausted_wall_clock_defers_everything(self):
        with patch.object(ca, "ADVISOR_BUDGET_MS", 0):
            state = ca._advisor_state(self._many_unassigned(3), "fp")
        self.assertTrue(state["truncated"])
        self.assertTrue(all(c["detected_only"]
                            for c in state["disruptions"]))

    def test_for_leg_id_narrows_to_that_legs_cards(self):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        u = _leg(701, 13, 30, driver_id=None)
        board = _board({1: [a, b]}, now=_dt(12, 0), extra_legs=[u])
        state = ca._advisor_state(board, "fp", for_leg_id=701)
        self.assertEqual([c["id"] for c in state["disruptions"]],
                         ["unassigned:701"])

    def test_determinism_two_runs_identical_output(self):
        def _mk():
            a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                     dropoff="Disney Contemporary")
            b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                     dropoff="Disney Boardwalk")
            u = _leg(701, 13, 30, driver_id=None)
            return _board({1: [a, b], 2: []}, now=_dt(12, 0), extra_legs=[u],
                          farm_ctx=_fake_farm_ctx())
        self.assertEqual(ca._advisor_state(_mk(), "fp"),
                         ca._advisor_state(_mk(), "fp"))

    def test_state_and_plan_contract_shapes(self):
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary")
        b = _leg(102, 15, 10, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [a, b], 2: []}, now=_dt(12, 0))
        state = ca._advisor_state(board, "fp")
        self.assertEqual(set(state), {"fingerprint", "computed_at",
                                      "truncated", "disruptions"})
        card = state["disruptions"][0]
        self.assertLessEqual(
            {"id", "kind", "severity", "headline", "narrative", "impact_at",
             "leg_ids", "task_id", "basis", "plans", "detected_only",
             "no_internal_solution"},
            set(card))
        plan = card["plans"][0]
        # `display` is the dispatcher-facing presentation block
        # (advisor_display.plan_display) — additive: every key below it is the
        # engine's own output, unchanged, and still drives apply + the card's
        # "Show the math" expander.
        self.assertEqual(
            set(plan),
            {"id", "key", "rank", "title", "why", "risks", "farm_out",
             "price_impact", "score", "moves", "apply", "display"})
        self.assertEqual(plan["id"], f"{card['id']}#p1")
        # `key` is the content-stable identity (guard-10 hysteresis input) —
        # the move signature, not the positional #p rank.
        self.assertTrue(plan["key"].startswith(("reassign|", "swap_chain|")),
                        plan["key"])
        self.assertIsInstance(plan["score"], int)
        self.assertEqual(
            set(plan["apply"]),
            {"schema", "date", "disruption_id", "plan_id", "task_id", "title",
             "actions", "expected", "expected_times"})
        self.assertEqual(plan["apply"]["title"], plan["title"])
        self.assertEqual(plan["apply"]["schema"], 1)
        self.assertEqual(plan["apply"]["date"], "2026-05-01")
        json.dumps(state)   # the whole contract must be JSON-serializable

    def test_monitor_plan_has_no_apply_payload(self):
        # Reality thinned a clean turn to 7 min (planned 17) — warning band,
        # recorded-pickup basis: guard 5 forbids moving work, so the card's
        # single plan is tier-0 monitor with NO apply payload.
        a = _leg(101, 14, 0, trip="arrival", pickup="MCO Terminal B",
                 dropoff="Disney Contemporary", status="picked-up")
        b = _leg(102, 15, 44, pickup="Disney Grand Floridian",
                 dropoff="Disney Boardwalk")
        board = _board({1: [a, b], 2: []}, now=_dt(15, 0),
                       picked={101: _dt(14, 55)})
        d = _one_card(board, "overlap")
        self.assertEqual(d.severity, "warning")
        state = ca._advisor_state(board, "fp")
        card = next(c for c in state["disruptions"]
                    if c["id"] == "overlap:101:102")
        self.assertEqual(len(card["plans"]), 1)
        plan = card["plans"][0]
        self.assertNotIn("apply", plan)
        self.assertIn("Monitor", plan["title"])
        self.assertFalse(card["no_internal_solution"])


# ════════════════════════════════════════════════════════════════════════════
# STAGE G — performance & isolation budgets (plan Verification section)
# ════════════════════════════════════════════════════════════════════════════
from django.db import connection as _connection
from django.test.utils import CaptureQueriesContext


def _real_queries(ctx):
    """Captured queries minus transaction bookkeeping (sqlite savepoints)."""
    return [q for q in ctx.captured_queries
            if "SAVEPOINT" not in q["sql"].upper()]


class _ScaleBoardFixture(TestCase):
    """A REAL-DB 80-leg / 25-driver day with comfortable spacing — the plan's
    synthetic perf board. Healthy by construction (no farm tier, no prev-tail),
    so the measured cost is the advisor's FIXED budget: fingerprint (3) +
    board assembly (12). Timing tables are empty -> founder-table fallbacks,
    zero per-pair metric queries (the 'mocked timing tables' of the plan)."""

    PERF_DAY = timezone.localdate() + timedelta(days=7)

    @classmethod
    def setUpTestData(cls):
        from rates.models import Location, Rate, Route, Vehicle
        from reservations.models import Customer, Leg, Reservation
        from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle

        vehicle = Vehicle.objects.create(vehicle_type="towncar", capacity=4,
                                         luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        route = Route.objects.create(origin=origin, destination=dest,
                                     inhouse_base_pay=Decimal("50.00"))
        rate = Rate.objects.create(vehicle=vehicle, route=route,
                                   oneway_price=Decimal("100.00"),
                                   round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone_number="5551234567")
        cls.vehicle, cls.rate = vehicle, rate
        cls.drivers = []
        for i in range(25):
            d = Driver.objects.create(
                profile=User.objects.create_user(f"perf-drv-{i}",
                                                 first_name=f"P{i}"),
                driver_type="inhouse")
            fleet = FleetVehicle.objects.create(
                vehicle_number=f"P-{i}", vehicle_type=vehicle, year=2024,
                make="Lincoln", model="Continental")
            DriverVehicleAssignment.objects.create(driver=d, date=cls.PERF_DAY,
                                                   vehicle=fleet)
            cls.drivers.append(d)
        # 80 legs round-robin over the 25 drivers: ~3/driver, 4 h apart on the
        # same driver — nothing tight anywhere.
        for n in range(80):
            cls._make_leg(dt_time(6 + (n // 25) * 4, (n % 25) * 2),
                          cls.drivers[n % 25])

    @classmethod
    def _make_leg(cls, pickup_time, driver, **kw):
        from reservations.models import Leg, Reservation
        res = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, vehicle=cls.vehicle,
            rate=cls.rate, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        defaults = dict(
            reservation=res, pickup_date=cls.PERF_DAY, pickup_time=pickup_time,
            pickup_location="MCO Terminal B",
            dropoff_location="Disney Contemporary",
            status="confirmed", driver=driver)
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class QueryBudgetTests(_ScaleBoardFixture):
    def test_full_compute_stays_inside_fifteen_queries(self):
        # The plan's pinned budget: fingerprint 3 + board assembly 12. A
        # regression here is the legs-prefetch N+1 (legstop_set /
        # reservation__payments — 2 x 80 queries before the Stage G fix) or a
        # builder re-querying the DVA table it was handed rows for.
        with CaptureQueriesContext(_connection) as ctx:
            state = ca.compute_advisor_state(self.PERF_DAY)
        self.assertLessEqual(len(_real_queries(ctx)), 15,
                             "\n".join(q["sql"][:120] for q in _real_queries(ctx)))
        # Healthy fixture => zero cards, so no lazy farm-tier queries hid
        # inside the measurement (and the budget test doubles as a scale-level
        # prime-directive echo).
        self.assertEqual(state["disruptions"], [])

    def test_full_compute_wall_clock_bounded(self):
        # Best of two runs: the suite's background-email thread can stall a
        # sqlite read on a one-off table lock, which is machine noise, not an
        # advisor regression — a REAL blowup fails both attempts.
        import time as _time
        best_ms = float("inf")
        for _ in range(2):
            t0 = _time.monotonic()
            ca.compute_advisor_state(self.PERF_DAY)
            best_ms = min(best_ms, (_time.monotonic() - t0) * 1000)
            if best_ms < ca.ADVISOR_BUDGET_MS:
                break
        self.assertLess(best_ms, ca.ADVISOR_BUDGET_MS,
                        f"full compute took {best_ms:.0f} ms on the 80-leg "
                        f"board (hard cap {ca.ADVISOR_BUDGET_MS} ms)")


class ExternalCallIsolationTests(_ScaleBoardFixture):
    """ZERO external calls anywhere in the advisor path (plan Verification):
    drivers.utils.get_drive_time (raw Google — the $593 incident),
    samsara_risk.evaluate* (live Samsara HTTP), and every requests-lib call
    (AeroAPI / Samsara / Google alike) are patched to RAISE. The compute must
    still succeed end-to-end — detection, generation, validation, ranking."""

    def _forbidden(self, name):
        def _raise(*a, **kw):
            raise AssertionError(f"advisor path invoked forbidden {name}")
        return _raise

    def test_disrupted_compute_makes_zero_external_calls(self):
        # Seed real disruptions so the FULL pipeline runs: an overlapping
        # same-driver pair (critical overlap, placed in the evening where the
        # other 24 drivers are free to receive) and an unassigned leg inside
        # the 120-min horizon.
        self._make_leg(dt_time(18, 0), self.drivers[0])
        self._make_leg(dt_time(18, 5), self.drivers[0])
        self._make_leg(dt_time(9, 30), None)
        now = timezone.make_aware(
            datetime.combine(self.PERF_DAY, dt_time(8, 0)))
        with patch("drivers.utils.get_drive_time",
                   side_effect=self._forbidden("drivers.utils.get_drive_time")), \
             patch("dispatching.samsara_risk.evaluate",
                   side_effect=self._forbidden("samsara_risk.evaluate")), \
             patch("dispatching.samsara_risk.evaluate_driver",
                   side_effect=self._forbidden("samsara_risk.evaluate_driver")), \
             patch("requests.sessions.Session.request",
                   side_effect=self._forbidden("requests HTTP (AeroAPI/Samsara/Google)")):
            state = ca.compute_advisor_state(self.PERF_DAY, now=now)
        kinds = {c["kind"] for c in state["disruptions"]}
        self.assertIn("overlap", kinds)
        self.assertIn("unassigned", kinds)
        # The overlap card produced ranked plans — generation + whole-board
        # validation ran entirely on cached/persisted inputs.
        overlap = next(c for c in state["disruptions"] if c["kind"] == "overlap")
        self.assertTrue(overlap["plans"])


class WindowEnforceCapSplitTests(_ScaleBoardFixture):
    def test_generation_windows_resolved_with_enforce_cap_true(self):
        # The risk-review split, generation half: the advisor is an AUTOMATIC
        # path — candidate generation must resolve every deployable window
        # with enforce_cap=True (never auto-build 18-hour days). The apply
        # half (enforce_cap=False, manual-sovereign) is pinned in
        # tests_conflict_advisor_apply.
        from dispatching import feasibility_guards as fg
        seen = []
        real = fg.get_effective_window

        def spy(driver_id, configured=None, enforce_cap=True):
            seen.append(enforce_cap)
            return real(driver_id, configured=configured,
                        enforce_cap=enforce_cap)

        with patch.object(fg, "get_effective_window", side_effect=spy):
            ca.build_board_state(self.PERF_DAY)
        self.assertEqual(len(seen), 25)          # one per deployable driver
        self.assertTrue(all(seen), "generation resolved a window with "
                                   "enforce_cap=False")
