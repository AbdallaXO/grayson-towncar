"""One definition of "late" — pins dispatching/pickup_policy and the surfaces that
now read from it.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_pickup_policy

Why this suite exists: the board used to carry six independent engines that each
answered "is this pickup at risk?" with different numbers, so a turn auto-assign
had just seated as legal could paint red the moment it hit the board. The cases
below are the specific contradictions that were live on 2026-07-31, written so a
regression is loud.
"""
from datetime import datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from dispatching import feasibility_guards as fg
from dispatching import pickup_policy as pp
from dispatching.views import _pickup_risk, _truthful_pill_span


def _dt(h, m=0):
    """Fixed naive-local datetime on an arbitrary day."""
    return datetime(2026, 7, 31, h, m)


class PolicyConstantTests(TestCase):
    """The numbers themselves. Founder rule 2026-07-31: the driver is due at the
    IN-TERMINAL commercial-lane meet point within 10 min of gate arrival."""

    def test_meet_grace_is_ten(self):
        self.assertEqual(pp.ARRIVAL_MEET_GRACE_MIN, 10)

    def test_pax_ready_stays_separate_from_the_meet_deadline(self):
        # Deliberately different numbers answering different questions. If these
        # ever collapse into one value, someone has conflated "when must the driver
        # be standing there" with "when can the car actually pull away".
        self.assertEqual(pp.PAX_READY_MIN, 15)
        self.assertNotEqual(pp.PAX_READY_MIN, pp.ARRIVAL_MEET_GRACE_MIN)

    def test_engine_grace_alias_still_ten(self):
        # feasibility_guards is the auto-assign engine; the board must agree with it.
        self.assertEqual(fg.DEPLANING_GRACE_MIN, pp.ARRIVAL_MEET_GRACE_MIN)


class SlackBandTests(TestCase):
    def test_negative_slack_is_at_risk(self):
        self.assertEqual(pp.classify_slack(-1), pp.AT_RISK)

    def test_thin_slack_is_watch(self):
        self.assertEqual(pp.classify_slack(0), pp.WATCH)
        self.assertEqual(pp.classify_slack(9), pp.WATCH)

    def test_comfortable_slack_is_on_time(self):
        self.assertEqual(pp.classify_slack(10), pp.ON_TIME)
        self.assertEqual(pp.classify_slack(45), pp.ON_TIME)

    def test_no_slack_information_is_unknown(self):
        self.assertEqual(pp.classify_slack(None), pp.UNKNOWN)


class TurnBandTests(TestCase):
    """Turnaround slack = gap − required_turnaround. Must match
    scheduler.check_feasibility exactly (negative infeasible, <15 "Tight")."""

    def test_negative_is_critical(self):
        self.assertEqual(pp.turn_band(-1), "critical")

    def test_under_fifteen_is_tight(self):
        self.assertEqual(pp.turn_band(0), "tight")
        self.assertEqual(pp.turn_band(14), "tight")

    def test_fifteen_and_up_is_clean(self):
        self.assertEqual(pp.turn_band(15), "")
        self.assertEqual(pp.turn_band(60), "")


class NotMovingTests(TestCase):
    """The live 2026-07-31 false amber: a Return leg (Disney Contemporary -> MCO),
    pickup 4:00 PM. At 3:39 the vehicle was 5 min away — 16 min of slack — and the
    board said "Pickup in 21 min, vehicle not moving". A driver waiting five
    minutes from his pickup is behaving correctly."""

    def test_parked_with_real_slack_is_not_flagged(self):
        self.assertFalse(pp.should_flag_not_moving(16, False))

    def test_parked_when_he_should_be_rolling_is_flagged(self):
        self.assertTrue(pp.should_flag_not_moving(5, False))
        self.assertTrue(pp.should_flag_not_moving(-2, False))

    def test_a_moving_vehicle_is_never_flagged(self):
        self.assertFalse(pp.should_flag_not_moving(1, True))

    def test_the_old_rule_would_have_flagged_the_live_case(self):
        # Guards the actual regression: the retired rule was "pickup within 30 min
        # and idle", which the live case satisfied (21 min out, parked).
        minutes_to_pickup, drive = 21, 5
        self.assertLessEqual(minutes_to_pickup, 30)          # old rule fired
        self.assertFalse(                                     # new rule does not
            pp.should_flag_not_moving(minutes_to_pickup - drive, False))


class OverdueStalenessTests(TestCase):
    """The 70m/108m-late pills. The GPS engine has always capped its past-pickup
    badge at 45 min; the clock flags never did."""

    def test_recent_overdue_is_live(self):
        self.assertFalse(pp.is_overdue_stale(20))
        self.assertFalse(pp.is_overdue_stale(45))

    def test_hours_overdue_is_stale(self):
        self.assertTrue(pp.is_overdue_stale(46))
        self.assertTrue(pp.is_overdue_stale(108))

    def test_pill_stops_flagging_once_stale(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 0), est_end_dt=_dt(14, 0),
            status='in-progress', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(14, 48), is_today=True)          # 108 min past pickup
        self.assertFalse(span['pickup_overdue'])
        self.assertFalse(span['pickup_stalled'])

    def test_stale_leg_still_is_not_drawn_as_a_busy_bar(self):
        # Expiring the FLAG must not make an un-started job look like it's running.
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 0), est_end_dt=_dt(14, 0),
            status='in-progress', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(14, 48), is_today=True)
        self.assertFalse(span['overrunning'])

    def test_overdue_within_the_window_still_flags(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 0), est_end_dt=_dt(14, 0),
            status='in-progress', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(13, 20), is_today=True)
        self.assertTrue(span['pickup_overdue'])
        self.assertTrue(span['pickup_stalled'])
        self.assertEqual(span['pickup_overdue_mins'], 20)


class FlightAwareDeadlineTests(TestCase):
    """A delayed flight must move the bar out, not report the driver late for a
    plane that hasn't landed."""

    def test_delayed_flight_is_not_overdue(self):
        # Booked 1:00 PM, flight now gates 2:30 PM so the pickup realistically
        # completes ~3:15. At 2:00 PM the old code read "60m late"; the plane is
        # still in the air.
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 0), est_end_dt=_dt(14, 0),
            status='in-progress', trip_type='arrival',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(14, 0), is_today=True,
            expected_pickup_dt=_dt(15, 15), is_flight_gated=True)
        self.assertFalse(span['pickup_overdue'])

    def test_past_the_moved_deadline_still_flags(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 0), est_end_dt=_dt(14, 0),
            status='in-progress', trip_type='arrival',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(15, 30), is_today=True,
            expected_pickup_dt=_dt(15, 15), is_flight_gated=True)
        self.assertTrue(span['pickup_overdue'])
        self.assertEqual(span['pickup_overdue_mins'], 15)

    def test_driver_waiting_at_baggage_claim_on_schedule_is_quiet(self):
        # The 2026-07-31 walkthrough: 2:00 PM gate, now 2:45 PM, driver still at the
        # airport with the guest not yet collected. That is EXACTLY on schedule —
        # ARRIVAL_DWELL_MIN says a 2:00 landing is in the car around 2:45. Judging it
        # against the driver's own 10-min meet deadline reported "35 min overdue".
        expected = _dt(14, 0) + timedelta(minutes=pp.ARRIVAL_DWELL_MIN)
        for status in ('confirmed', 'on-location', 'on-the-way'):
            span = _truthful_pill_span(
                sched_start_dt=_dt(14, 0), est_end_dt=_dt(15, 15),
                status=status, trip_type='arrival',
                picked_up_dt=None, completed_dt=None,
                now_dt=_dt(14, 45), is_today=True,
                expected_pickup_dt=expected, is_flight_gated=True)
            self.assertFalse(span['pickup_overdue'], msg=f"status={status}")
            self.assertFalse(span['pickup_stalled'], msg=f"status={status}")

    def test_airport_pickup_still_flags_once_genuinely_late(self):
        # Same leg, but now 3:20 — well past even the dwell allowance and still
        # nothing recorded. That IS worth surfacing.
        expected = _dt(14, 0) + timedelta(minutes=pp.ARRIVAL_DWELL_MIN)
        span = _truthful_pill_span(
            sched_start_dt=_dt(14, 0), est_end_dt=_dt(15, 15),
            status='confirmed', trip_type='arrival',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(15, 20), is_today=True,
            expected_pickup_dt=expected, is_flight_gated=True)
        self.assertTrue(span['pickup_overdue'])
        self.assertTrue(span['pickup_stalled'])
        self.assertEqual(span['pickup_overdue_mins'], 35)

    def test_expected_pickup_uses_dwell_not_the_meet_grace(self):
        # The two clocks must not be confused: the driver is due at gate+10, but the
        # job isn't expected to have started until gate+45.
        self.assertEqual(pp.ARRIVAL_DWELL_MIN, 45)
        self.assertGreater(pp.ARRIVAL_DWELL_MIN, pp.ARRIVAL_MEET_GRACE_MIN)

    def test_flight_gated_pickup_never_counts_as_a_late_start(self):
        # A cruise transfer out of MCO reads trip_type='cruise' but waits on a plane
        # exactly like an arrival — is_flight_gated, not trip_type, decides.
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 0), est_end_dt=_dt(14, 0),
            status='picked-up', trip_type='cruise',
            picked_up_dt=_dt(13, 40), completed_dt=None,
            now_dt=_dt(14, 0), is_today=True,
            is_flight_gated=True)
        self.assertFalse(span['late_start'])

    def test_a_real_departure_still_reports_a_late_start(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 0), est_end_dt=_dt(14, 0),
            status='picked-up', trip_type='return',
            picked_up_dt=_dt(13, 40), completed_dt=None,
            now_dt=_dt(14, 0), is_today=True,
            is_flight_gated=False)
        self.assertTrue(span['late_start'])
        self.assertEqual(span['late_start_mins'], 40)


class AirportCruiseGraceTests(TestCase):
    """An airport -> cruise-port transfer is functionally an arrival: the driver is
    meeting a plane. It was the one flight-gated leg the turnaround guard refused
    to credit, because get_trip_type() labels it 'cruise' for its dropoff."""

    def test_cruise_leg_out_of_a_terminal_counts_as_an_arrival(self):
        self.assertTrue(fg.is_airport_arrival("cruise", "MCO Terminal"))

    def test_plain_arrival_still_counts(self):
        self.assertTrue(fg.is_airport_arrival("arrival", "MCO Terminal"))

    def test_from_cruise_leg_does_not(self):
        # Picks up at the port, not a terminal — no plane to meet.
        self.assertFalse(fg.is_airport_arrival("cruise", "Port Canaveral Area"))

    def test_a_return_is_never_an_arrival(self):
        self.assertFalse(fg.is_airport_arrival("return", "MCO Terminal"))

    def test_same_terminal_cruise_turn_gets_the_grace(self):
        # Drop a return at MCO, grab an MCO -> Port Canaveral transfer: the guests
        # are still deplaning, so the required turnaround is negative.
        self.assertEqual(
            fg.required_turnaround(45, fg.is_airport_arrival("cruise", "MCO Terminal"),
                                   same_terminal=True),
            -pp.ARRIVAL_MEET_GRACE_MIN)

    def test_repositioning_in_earns_no_grace(self):
        # Coming from a resort he must actually drive; the deplaning window cannot
        # pay for 25 minutes of driving.
        self.assertEqual(
            fg.required_turnaround(25, next_is_airport_arrival=True, same_terminal=False),
            25)


class ActualPickupReanchorTests(TestCase):
    """The board's gap chip must believe a RECORDED pickup over the planning model.

    Scenario walked through on 2026-07-31: 2:00 PM gate, MCO -> Disney, then a 3:30
    Disney pickup. The plan says leg A clears 3:15 (gate + 45 dwell + 30 drive), which
    left 3 min of slack and an amber chip. But the driver tapped picked-up at 2:30 —
    the dwell is spent, it is no longer an estimate — so he really clears ~3:00 and
    has 18 min. The live GPS badge already said on-time; the chip contradicted it.
    """

    def _leg(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            pickup_location="MCO Terminal B",
            dropoff_location="Disney's Contemporary Resort",
            get_trip_type=lambda: "arrival",
            reservation=SimpleNamespace(store_stop=False),
        )

    def test_recorded_pickup_reanchors_the_clear(self):
        from dispatching.scheduler import chain_clear_dt_from_actual
        clear = chain_clear_dt_from_actual(self._leg(), _dt(14, 30))
        self.assertEqual(clear, _dt(15, 0))          # 2:30 + 30 min MCO->Disney

    def test_reanchored_turn_is_no_longer_tight(self):
        from dispatching.scheduler import chain_clear_dt_from_actual
        clear = chain_clear_dt_from_actual(self._leg(), _dt(14, 30))
        repo = 12                                    # Disney -> Disney
        req = fg.required_turnaround(repo, next_is_airport_arrival=False,
                                     same_terminal=True)
        slack = int((_dt(15, 30) - (clear + timedelta(minutes=req))).total_seconds() / 60)
        self.assertEqual(slack, 18)
        self.assertEqual(pp.turn_band(slack), "")    # was 'tight' on the plan alone

    def test_the_plan_alone_would_still_have_said_tight(self):
        # Guards the regression: without the re-anchor this turn reads amber.
        repo = 12
        req = fg.required_turnaround(repo, next_is_airport_arrival=False,
                                     same_terminal=True)
        slack = int((_dt(15, 30) - (_dt(15, 15) + timedelta(minutes=req))).total_seconds() / 60)
        self.assertEqual(slack, 3)
        self.assertEqual(pp.turn_band(slack), "tight")

    def test_a_late_pickup_makes_the_turn_worse_not_better(self):
        # The re-anchor is not a way to make things look good — it cuts both ways.
        # Picked up at 3:00 (late) => clears 3:30 => he cannot make the 3:30 pickup.
        from dispatching.scheduler import chain_clear_dt_from_actual
        clear = chain_clear_dt_from_actual(self._leg(), _dt(15, 0))
        req = fg.required_turnaround(12, next_is_airport_arrival=False, same_terminal=True)
        slack = int((_dt(15, 30) - (clear + timedelta(minutes=req))).total_seconds() / 60)
        self.assertEqual(pp.turn_band(slack), "critical")

    def test_no_recorded_pickup_keeps_the_conservative_plan(self):
        # Nothing tapped => no fact to believe => the planning model stands. This is
        # what keeps the chip from going quietly optimistic on a job that may not have
        # started at all.
        from dispatching.views import _gap_turn_slack
        self.assertIsNone(_gap_turn_slack(None, None, _dt(0, 0).date()))

    def test_board_status_maps_carry_the_recorded_pickup(self):
        """The re-anchor is only as good as the data reaching it. Both gap-chip call
        sites read 'picked_up_dt' out of their status map; if a map stops populating
        it the chip silently falls back to the plan and this whole fix evaporates with
        no test failing. Pin the key at the source."""
        import inspect
        from dispatching import views
        src = inspect.getsource(views)
        # index() (dispatch board) and daily_capacity_planner() each build one.
        self.assertGreaterEqual(
            src.count("'picked_up_dt': _picked_up_local"), 1,
            "index()/schedule_board status map stopped recording the actual pickup")
        self.assertIn("'picked_up_dt': _cp_picked_up_local", src,
                      "capacity-planner status map stopped recording the actual pickup")


class RiskPrecedenceTests(TestCase):
    """_pickup_risk folds live GPS and the clock into one cue. GPS is the truth
    when it's fresh; the clock is a fallback for when there's no telematics."""

    def _r(self, **kw):
        base = dict(pickup_overdue=False, pickup_stalled=False, overdue_mins=0,
                    gps_status='', gps_eta_mins=None, gps_reason='')
        base.update(kw)
        return _pickup_risk(**base)

    def test_healthy_gps_suppresses_a_stale_clock_flag(self):
        # The regression: a driver who simply hadn't tapped "on the way" showed a
        # critical red even though telemetry said he was comfortably positioned.
        r = self._r(gps_status='on_time', gps_eta_mins=4,
                    pickup_overdue=True, pickup_stalled=True, overdue_mins=7)
        self.assertEqual(r['tier'], '')

    def test_healthy_gps_suppresses_plain_overdue_too(self):
        r = self._r(gps_status='on_time', gps_eta_mins=4,
                    pickup_overdue=True, pickup_stalled=False, overdue_mins=7)
        self.assertEqual(r['tier'], '')

    def test_unknown_gps_does_not_suppress(self):
        # No signal is not a clean bill of health.
        r = self._r(gps_status='unknown',
                    pickup_overdue=True, pickup_stalled=True, overdue_mins=7)
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['source'], 'clock')

    def test_absent_gps_does_not_suppress(self):
        r = self._r(pickup_overdue=True, pickup_stalled=True, overdue_mins=7)
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['source'], 'clock')

    def test_gps_at_risk_still_wins(self):
        r = self._r(gps_status='at_risk', gps_eta_mins=30,
                    pickup_overdue=True, pickup_stalled=True, overdue_mins=5)
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['source'], 'gps')


class ControllingFlightQueryTests(TestCase):
    """pickup_deadline reads the controlling flight for every leg on the board.
    Leg.controlling_flight does legflight_set.filter(...), and a .filter() on a
    related manager always bypasses the prefetch cache — that's a per-leg query on a
    ~140-leg board. pickup_policy.controlling_flight must read the cache instead."""

    def setUp(self):
        from decimal import Decimal
        from rates.models import Location, Rate, Route, Vehicle
        from reservations.models import Customer, Flight, LegFlight, Reservation, Leg

        self.cust = Customer.objects.create(first_name="A", last_name="B",
                                            email="pp@example.com",
                                            phone_number="5550001111")
        veh = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=4)
        o = Location.objects.create(name="MCO")
        d = Location.objects.create(name="Disney")
        self.route = Route.objects.create(origin=o, destination=d,
                                          inhouse_base_pay=Decimal("50.00"))
        rate = Rate.objects.create(route=self.route, vehicle=veh,
                                   oneway_price=Decimal("100.00"),
                                   round_trip_price=Decimal("180.00"))
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.cust, vehicle=veh, rate=rate,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        self.leg = Leg.objects.create(
            reservation=res, pickup_date=timezone.localdate(), pickup_time=time(10, 30),
            pickup_location="MCO Terminal B", dropoff_location="Disney's Contemporary Resort",
            route=self.route, status="confirmed")
        flight = Flight.objects.create(
            flight_type="arrival", airline="AA", flight_number="2069",
            scheduled_gate_arrival_local=timezone.make_aware(
                datetime.combine(timezone.localdate(), time(10, 30))))
        LegFlight.objects.create(leg=self.leg, flight=flight, is_controlling=True, sequence=1)

    def test_prefetched_leg_costs_no_extra_query(self):
        from reservations.models import Leg
        legs = list(Leg.objects.filter(pk=self.leg.pk)
                    .select_related("flight_information")
                    .prefetch_related("legflight_set__flight"))
        with self.assertNumQueries(0):
            self.assertIsNotNone(pp.controlling_flight(legs[0]))

    def test_deadline_is_gate_plus_the_meet_grace(self):
        from reservations.models import Leg
        leg = (Leg.objects.filter(pk=self.leg.pk)
               .prefetch_related("legflight_set__flight").first())
        deadline, basis = pp.pickup_deadline(leg, aware=False)
        self.assertEqual(deadline.time(), time(10, 40))   # 10:30 gate + 10
        self.assertIn("10:30", basis)
        self.assertIn("10:40", basis)

    def test_expected_pickup_is_gate_plus_dwell(self):
        from reservations.models import Leg
        leg = (Leg.objects.filter(pk=self.leg.pk)
               .prefetch_related("legflight_set__flight").first())
        expected, basis = pp.pickup_expected_dt(leg, aware=False)
        self.assertEqual(expected.time(), time(11, 15))   # 10:30 gate + 45
        self.assertIn("10:30", basis)


class TightTurnThresholdTests(TestCase):
    """ops.tasks.classify_turn bands minutes past the RAW flight arrival. Under the
    gate+10 rule: at or under the grace he still makes it (amber); past it he
    doesn't (red)."""

    def test_red_threshold_tracks_the_meet_grace(self):
        from ops.tasks import TIGHT_TURN_RED_AFTER_MIN
        self.assertEqual(TIGHT_TURN_RED_AFTER_MIN, pp.ARRIVAL_MEET_GRACE_MIN)

    def test_exactly_at_the_grace_is_amber_not_red(self):
        # He is inside by gate+10 — that is the rule, so this is "no margin", not late.
        from ops.tasks import TIGHT_TURN_RED_AFTER_MIN
        late = 10
        self.assertEqual("red" if late > TIGHT_TURN_RED_AFTER_MIN else "amber", "amber")

    def test_past_the_grace_is_red(self):
        from ops.tasks import TIGHT_TURN_RED_AFTER_MIN
        for late in (11, 15, 40):
            self.assertEqual(
                "red" if late > TIGHT_TURN_RED_AFTER_MIN else "amber", "red",
                msg=f"{late} min past the plane should be red")
