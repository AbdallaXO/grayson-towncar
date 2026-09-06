"""ETA sample tests — keeping what the Samsara sweep already knows (Phase 1.3).

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_eta_samples

What is pinned here and why:

  * THE WRITE RULE IS MEASURED, NOT CHOSEN. "Under way OR within an hour of the
    moment being measured" halves the volume of a literal per-tick insert while
    keeping 97% of the samples any analysis can grade and every ambiguous leg
    §3.4 wants GPS for. Both halves of the OR are pinned, because dropping
    either one loses a different thing and neither loss would raise anything.
  * THE SAMPLE IS THE SWEEP'S OWN ARITHMETIC. slack = minutes_to_target -
    eta_minutes, against the same `now` the sweep stamps as evaluated_at. A
    second opinion here would put the log and the leg row quietly at odds.
  * ORDER MATTERS. build_sample must run BEFORE _apply_eta_fields, because the
    leg still holds the previous tick's ETA and origin — the only evidence that
    this tick's value carries no new information.
  * THE SWEEP NEVER GOES DARK. The ETA badges the board depends on must survive
    any failure in the log.
  * 07's TABLE IS REPRODUCIBLE from the sample shape. That is the ticket's
    stated gate, and analysis/28 --verify-fill runs the same reader over 3,000
    real predictions; these are the unit form of the same claim.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from dispatching import eta_samples as es
from dispatching.models import DispatchEtaSample
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate()
NOW = timezone.make_aware(datetime.combine(DAY, time(9, 0)))


def _fields(**kw):
    """The dict evaluate() hands back, in its shipped shape."""
    f = {
        "dispatch_eta_minutes": 12,
        "dispatch_eta_target": "pickup",
        "dispatch_eta_target_time": NOW + timedelta(minutes=30),
        "dispatch_risk_status": "on_time",
        "dispatch_risk_reason": "18 min slack",
        "dispatch_is_moving": True,
        "dispatch_stationary_minutes": 0,
        "dispatch_vehicle_label": "T-1",
        "dispatch_eta_origin_lat": 28.42,
        "dispatch_eta_origin_lng": -81.31,
        "dispatch_eta_origin_target": "MCO",
    }
    f.update(kw)
    return f


def _leg(**kw):
    """A leg-shaped stand-in carrying last tick's dispatch_* values."""
    d = dict(id=1, driver_id=7, status="on-the-way", dispatch_eta_minutes=None,
             dispatch_eta_origin_lat=None, dispatch_eta_origin_lng=None,
             dispatch_eta_target="")
    d.update(kw)
    return SimpleNamespace(**d)


class WriteRuleTests(TestCase):
    def test_a_started_driver_is_always_kept(self):
        for status in es.STARTED_STATUSES:
            self.assertTrue(es.wanted(status, minutes_to_target=600), status)

    def test_the_started_statuses_match_the_sweeps_own(self):
        """The sweep's chain math splits on these; if the two lists drift, the
        log starts sampling a different population than the one it describes."""
        from dispatching import samsara_risk as sr
        self.assertTrue(set(sr._ON_TRIP).issubset(set(es.STARTED_STATUSES)))

    def test_a_parked_driver_far_from_his_next_job_is_dropped(self):
        """Three quarters of a literal per-tick insert is this row — a car
        hours from its next pickup, which no analysis in this project scores."""
        self.assertFalse(es.wanted("confirmed", minutes_to_target=180))

    def test_a_parked_driver_inside_the_hour_is_kept(self):
        self.assertTrue(es.wanted("confirmed",
                                  minutes_to_target=es.NEAR_TARGET_MIN))
        self.assertFalse(es.wanted("confirmed",
                                   minutes_to_target=es.NEAR_TARGET_MIN + 0.1))

    def test_an_untapped_leg_near_its_deadline_is_kept(self):
        """The half of the rule that 'under way only' loses: an ambiguous leg —
        milestone passed, no pickup tap — is by construction NOT under way, and
        it is exactly the leg §3.4 wants GPS to speak about."""
        self.assertTrue(es.wanted("confirmed", minutes_to_target=5))
        self.assertTrue(es.wanted("", minutes_to_target=-10))

    def test_a_dropoff_target_has_no_deadline_so_only_status_can_keep_it(self):
        self.assertTrue(es.wanted("picked-up", minutes_to_target=None))
        self.assertFalse(es.wanted("confirmed", minutes_to_target=None))


class BuildSampleTests(TestCase):
    def test_the_sample_carries_the_sweeps_own_slack(self):
        """slack = minutes_to_target - eta_minutes (samsara_risk.evaluate),
        against the same `now` the sweep stamps as evaluated_at. The sweep keeps
        neither number — they survive today only as English in the reason."""
        s = es.build_sample(_leg(), _fields(), NOW)
        self.assertEqual(s.minutes_to_target, 30.0)
        self.assertEqual(s.slack_minutes, 18.0)
        self.assertEqual(s.eta_target, "pickup")
        self.assertEqual(s.risk_status, "on_time")
        self.assertEqual((s.leg_id_ref, s.driver_id_ref), (1, 7))
        self.assertEqual(s.sampled_at, NOW)

    def test_a_dropoff_has_no_deadline_and_therefore_no_slack(self):
        s = es.build_sample(_leg(status="picked-up"),
                            _fields(dispatch_eta_target="dropoff",
                                    dispatch_eta_target_time=None,
                                    dispatch_risk_status=""), NOW)
        self.assertIsNone(s.minutes_to_target)
        self.assertIsNone(s.slack_minutes)
        self.assertEqual(s.risk_status, "")

    def test_a_tick_the_rule_rejects_returns_nothing(self):
        self.assertIsNone(es.build_sample(
            _leg(status="confirmed"),
            _fields(dispatch_eta_target_time=NOW + timedelta(hours=3)), NOW))

    def test_the_movement_snapshot_survives(self):
        """§3.4: GPS's real job is not predicting an ETA, it is saying whether a
        car with no pickup tap has left the pickup point."""
        s = es.build_sample(_leg(), _fields(dispatch_is_moving=False,
                                            dispatch_stationary_minutes=23), NOW)
        self.assertIs(s.is_moving, False)
        self.assertEqual(s.stationary_minutes, 23)

    def test_a_first_sample_is_never_marked_carried(self):
        self.assertFalse(es.build_sample(_leg(), _fields(), NOW).eta_carried)

    def test_an_unchanged_value_from_an_unchanged_anchor_is_carried(self):
        """Not 'no Google call was made' — that is not knowable from the data.
        Same number, same anchor, same target means no new information about the
        road, which is what an analysis needs: 07's error formula treats
        sampled_at as the instant the drive time was measured, and here it is
        not."""
        prev = _leg(dispatch_eta_minutes=12, dispatch_eta_origin_lat=28.42,
                    dispatch_eta_origin_lng=-81.31, dispatch_eta_target="pickup")
        self.assertTrue(es.build_sample(prev, _fields(), NOW).eta_carried)

    def test_a_decimal_anchor_from_the_database_still_compares_equal(self):
        """Leg.dispatch_eta_origin_lat/lng are DecimalFields, so the previous
        tick's value comes back as a Decimal while the sweep's is a float.
        Comparing them raw makes eta_carried permanently False — a column that
        is always the same value and tells you nothing, with nothing to show
        for it."""
        from decimal import Decimal as D
        prev = _leg(dispatch_eta_minutes=12,
                    dispatch_eta_origin_lat=D("28.42"),
                    dispatch_eta_origin_lng=D("-81.31"),
                    dispatch_eta_target="pickup")
        self.assertTrue(es.build_sample(prev, _fields(), NOW).eta_carried)

    def test_a_moved_anchor_is_not_carried_even_at_the_same_minutes(self):
        prev = _leg(dispatch_eta_minutes=12, dispatch_eta_origin_lat=28.99,
                    dispatch_eta_origin_lng=-81.31, dispatch_eta_target="pickup")
        self.assertFalse(es.build_sample(prev, _fields(), NOW).eta_carried)

    def test_a_changed_target_is_not_carried(self):
        prev = _leg(dispatch_eta_minutes=12, dispatch_eta_origin_lat=28.42,
                    dispatch_eta_origin_lng=-81.31, dispatch_eta_target="dropoff")
        self.assertTrue(es.build_sample(prev, _fields(), NOW).eta_carried is False)


class RecordTests(TestCase):
    def test_rows_land_and_a_repeated_tick_is_harmless(self):
        s = es.build_sample(_leg(), _fields(), NOW)
        self.assertEqual(es.record_samples([s]), 1)
        again = es.build_sample(_leg(), _fields(), NOW)
        es.record_samples([again])
        self.assertEqual(DispatchEtaSample.objects.count(), 1)

    def test_a_write_failure_is_swallowed(self):
        with patch.object(DispatchEtaSample.objects, "bulk_create",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(
                es.record_samples([es.build_sample(_leg(), _fields(), NOW)]), 0)

    def test_the_switch_turns_it_off(self):
        with patch.object(es, "ETA_SAMPLES_ENABLED", False):
            es.record_samples([es.build_sample(_leg(), _fields(), NOW)])
        self.assertEqual(DispatchEtaSample.objects.count(), 0)

    def test_pruning_is_a_no_op_at_the_shipped_default(self):
        es.record_samples([es.build_sample(_leg(), _fields(), NOW)])
        self.assertEqual(es.RETENTION_DAYS, 0)
        self.assertEqual(es.prune(), 0)
        self.assertEqual(DispatchEtaSample.objects.count(), 1)

    def test_pruning_drops_only_what_is_older_than_the_window(self):
        old = es.build_sample(_leg(), _fields(), NOW - timedelta(days=40))
        new = es.build_sample(_leg(id=2), _fields(), NOW)
        es.record_samples([old, new])
        with patch.object(es, "RETENTION_DAYS", 30):
            self.assertEqual(es.prune(now=NOW), 1)
        self.assertEqual(DispatchEtaSample.objects.count(), 1)


class PredictionErrorsTests(TestCase):
    """07's ETA-error table, from the sample shape. analysis/28 --verify-fill
    runs this same reader over 3,000 real predictions."""

    def _s(self, **kw):
        d = dict(leg_id_ref=1, sampled_at=NOW, eta_minutes=10,
                 eta_target="pickup", risk_status="on_time")
        d.update(kw)
        return SimpleNamespace(**d)

    def test_the_error_is_signed_the_way_07_signs_it(self):
        """NEGATIVE = the system said he would get there EARLIER than he did."""
        taps = {1: {"on-the-way": NOW - timedelta(minutes=5),
                    "on-location": NOW + timedelta(minutes=25)}}
        rows = es.prediction_errors([self._s()], taps, {1: DAY}, DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "en route -> PICKUP")
        self.assertEqual(rows[0][7], -15.0)

    def test_a_sample_outside_the_tap_window_is_dropped(self):
        taps = {1: {"on-the-way": NOW + timedelta(minutes=1),
                    "on-location": NOW + timedelta(minutes=25)}}
        self.assertEqual(es.prediction_errors([self._s()], taps, {1: DAY}, DAY), [])

    def test_a_leg_with_no_tap_pair_is_dropped(self):
        self.assertEqual(
            es.prediction_errors([self._s()], {1: {"on-the-way": NOW}},
                                 {1: DAY}, DAY), [])

    def test_a_forward_dated_leg_is_dropped(self):
        taps = {1: {"on-the-way": NOW - timedelta(minutes=5),
                    "on-location": NOW + timedelta(minutes=25)}}
        self.assertEqual(
            es.prediction_errors([self._s()], taps, {1: DAY + timedelta(days=1)},
                                 DAY), [])

    def test_the_dropoff_case_scores_against_its_own_tap_pair(self):
        taps = {1: {"picked-up": NOW - timedelta(minutes=2),
                    "completed": NOW + timedelta(minutes=20)}}
        rows = es.prediction_errors([self._s(eta_target="dropoff",
                                             risk_status="")],
                                    taps, {1: DAY}, DAY)
        self.assertEqual(rows[0][0], "on trip -> DROPOFF")
        self.assertEqual(rows[0][7], -10.0)

    def test_next_pickup_is_scored_in_the_pickup_case(self):
        """target='next_pickup' is written only by the mid-trip branch — the
        chained signal 07 scores at 72% on 'late at all'."""
        taps = {1: {"on-the-way": NOW - timedelta(minutes=5),
                    "on-location": NOW + timedelta(minutes=25)}}
        rows = es.prediction_errors([self._s(eta_target="next_pickup")],
                                    taps, {1: DAY}, DAY)
        self.assertEqual(rows[0][0], "en route -> PICKUP")

    def test_two_samples_at_the_same_instant_collapse_to_one(self):
        taps = {1: {"on-the-way": NOW - timedelta(minutes=5),
                    "on-location": NOW + timedelta(minutes=25)}}
        rows = es.prediction_errors([self._s(), self._s()], taps, {1: DAY}, DAY)
        self.assertEqual(len(rows), 1)


class SweepIntegrationTests(TestCase):
    """The sweep writes samples and survives the log failing."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="J", last_name="D", email="j@example.com",
            phone_number="5551230000")
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user("es_sam", first_name="Sam"),
            driver_type="inhouse")
        cls.fleet = FleetVehicle.objects.create(
            vehicle_number="ES-1", vehicle_type=cls.vehicle, year=2024,
            make="Lincoln", model="Continental", samsara_vehicle_id="v1")
        DriverVehicleAssignment.objects.create(
            driver=cls.driver, date=DAY, vehicle=cls.fleet)
        res = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, rate=cls.rate,
            vehicle=cls.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        cls.leg = Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=time(9, 30),
            pickup_location="MCO", dropoff_location="Disney", route=cls.route,
            status="on-the-way", driver=cls.driver)

    def test_the_sweep_writes_a_sample_for_the_leg_it_flags(self):
        from dispatching import samsara_scheduler as ss
        with patch("dispatching.samsara_risk.evaluate_driver",
                   return_value={self.leg.id: _fields(
                       dispatch_eta_target_time=NOW + timedelta(minutes=30))}):
            ss.sweep_eta(now=NOW)
        s = DispatchEtaSample.objects.get()
        self.assertEqual(s.leg_id_ref, self.leg.id)
        self.assertEqual(s.slack_minutes, 18.0)
        self.assertFalse(s.eta_carried)

    def test_the_second_tick_sees_the_first_ticks_values_and_marks_it_carried(self):
        """The order guarantee: build_sample runs before _apply_eta_fields, so
        the leg still holds the previous tick's ETA and origin."""
        from dispatching import samsara_scheduler as ss
        f = _fields(dispatch_eta_target_time=NOW + timedelta(minutes=30))
        with patch("dispatching.samsara_risk.evaluate_driver",
                   return_value={self.leg.id: f}):
            ss.sweep_eta(now=NOW)
            ss.sweep_eta(now=NOW + timedelta(minutes=3))
        carried = list(DispatchEtaSample.objects.order_by("sampled_at")
                       .values_list("eta_carried", flat=True))
        self.assertEqual(carried, [False, True])

    def test_the_eta_badges_survive_a_broken_log(self):
        from dispatching import samsara_scheduler as ss
        with patch("dispatching.samsara_risk.evaluate_driver",
                   return_value={self.leg.id: _fields(
                       dispatch_eta_target_time=NOW + timedelta(minutes=30))}), \
             patch("dispatching.eta_samples.build_sample",
                   side_effect=RuntimeError("boom")):
            out = ss.sweep_eta(now=NOW)
        self.assertEqual(out["status"], "ok")
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.dispatch_eta_minutes, 12)
        self.assertEqual(DispatchEtaSample.objects.count(), 0)
