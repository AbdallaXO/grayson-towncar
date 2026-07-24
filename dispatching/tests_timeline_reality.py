"""Truthful timeline pills — the Schedule Board draws a pill from REALITY wherever
reality is known, and from the estimate only for what hasn't happened yet.

Run with:  ./manage.py test dispatching.tests_timeline_reality

The geometry decision lives in the pure ``views._truthful_pill_span`` helper so the
rules can be pinned with a controlled clock (the overrun rule is "now"-relative and
would otherwise be untestable). A thin integration test then confirms the schedule
board view wires a completed leg's actual clear time through to the rendered slot.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import preload_timing_cache
from dispatching.views import _pickup_risk, _truthful_pill_span
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, LegStatus, Reservation


def _dt(h, m=0):
    """A fixed naive-local datetime on an arbitrary day (the helper is date-agnostic)."""
    return datetime(2026, 7, 24, h, m)


class TruthfulPillSpanTests(TestCase):
    """Unit tests for the pure geometry helper — no DB, deterministic clock."""

    # ── Rule 2: clamp a completed pill to the ACTUAL cleared time ────────────
    def test_completed_clears_early_shrinks_to_actual(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 0), est_end_dt=_dt(16, 30),
            status='completed', trip_type='return',
            picked_up_dt=None, completed_dt=_dt(16, 5),
            now_dt=_dt(18, 0), is_today=True)
        self.assertTrue(span['cleared_is_actual'])
        self.assertEqual(span['eff_end'], _dt(16, 5))   # shrank from 4:30 to 4:05
        self.assertFalse(span['overrunning'])

    def test_completed_clears_late_grows_to_actual(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 0), est_end_dt=_dt(16, 30),
            status='completed', trip_type='return',
            picked_up_dt=None, completed_dt=_dt(16, 50),
            now_dt=_dt(18, 0), is_today=True)
        self.assertTrue(span['cleared_is_actual'])
        self.assertEqual(span['eff_end'], _dt(16, 50))   # grew from 4:30 to 4:50

    def test_completed_without_timestamp_falls_back_to_estimate(self):
        # Completed but no 'completed' status row recorded — no reality to clamp to.
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 0), est_end_dt=_dt(16, 30),
            status='completed', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(18, 0), is_today=True)
        self.assertFalse(span['cleared_is_actual'])
        self.assertEqual(span['eff_end'], _dt(16, 30))

    # ── Rule 1: extend an open, overdue pill to NOW (never end in the past) ──
    def test_overrunning_extends_to_now(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 0),
            status='picked-up', trip_type='arrival',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(14, 20), is_today=True)
        self.assertTrue(span['overrunning'])
        self.assertEqual(span['eff_end'], _dt(14, 20))   # extended past 2:00 to now
        self.assertEqual(span['overrun_mins'], 20)

    def test_not_overrunning_before_estimate_passes(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 0),
            status='picked-up', trip_type='arrival',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(13, 50), is_today=True)   # now is before the estimate
        self.assertFalse(span['overrunning'])
        self.assertEqual(span['eff_end'], _dt(14, 0))

    def test_overrun_only_on_todays_board(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 0),
            status='picked-up', trip_type='arrival',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(14, 20), is_today=False)   # a past/future date board
        self.assertFalse(span['overrunning'])
        self.assertEqual(span['eff_end'], _dt(14, 0))

    def test_cancelled_never_overruns(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 0),
            status='cancelled', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(14, 20), is_today=True)
        self.assertFalse(span['overrunning'])

    # ── Rule 3: shift a late-started DEPARTURE's left edge to the actual pickup ─
    def test_late_departure_shifts_left_edge(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 15),
            status='picked-up', trip_type='return',
            picked_up_dt=_dt(13, 50), completed_dt=None,
            now_dt=_dt(13, 55), is_today=True)
        self.assertTrue(span['late_start'])
        self.assertEqual(span['late_start_mins'], 20)
        self.assertEqual(span['eff_start'], _dt(13, 50))   # shifted from 1:30 to 1:50
        self.assertEqual(span['actual_pickup_dt'], _dt(13, 50))

    def test_arrival_late_pickup_is_never_flagged(self):
        # Arrivals are flight-gated: a "late" airport pickup is the flight, not the driver.
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 15),
            status='picked-up', trip_type='arrival',
            picked_up_dt=_dt(13, 50), completed_dt=None,
            now_dt=_dt(13, 55), is_today=True)
        self.assertFalse(span['late_start'])
        self.assertEqual(span['eff_start'], _dt(13, 30))   # unshifted

    def test_small_slip_under_threshold_not_flagged(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 15),
            status='picked-up', trip_type='return',
            picked_up_dt=_dt(13, 34), completed_dt=None,   # 4 min < 10 min threshold
            now_dt=_dt(13, 40), is_today=True)
        self.assertFalse(span['late_start'])

    def test_early_pickup_is_not_a_late_start(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 15),
            status='picked-up', trip_type='return',
            picked_up_dt=_dt(13, 22), completed_dt=None,   # picked up 8 min early
            now_dt=_dt(13, 40), is_today=True)
        self.assertFalse(span['late_start'])
        self.assertEqual(span['eff_start'], _dt(13, 30))

    def test_completed_late_departure_uses_both_actuals(self):
        # Late start AND clamped clear: the pill occupies the driver's real time on road.
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 15),
            status='completed', trip_type='return',
            picked_up_dt=_dt(13, 50), completed_dt=_dt(14, 40),
            now_dt=_dt(15, 0), is_today=True)
        self.assertTrue(span['late_start'])
        self.assertTrue(span['cleared_is_actual'])
        self.assertEqual(span['eff_start'], _dt(13, 50))
        self.assertEqual(span['eff_end'], _dt(14, 40))

    def test_no_status_data_is_pure_estimate(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(9, 0), est_end_dt=_dt(9, 45),
            status='confirmed', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(8, 0), is_today=True)
        self.assertEqual(span['eff_start'], _dt(9, 0))
        self.assertEqual(span['eff_end'], _dt(9, 45))
        self.assertFalse(span['late_start'])
        self.assertFalse(span['overrunning'])
        self.assertFalse(span['cleared_is_actual'])

    def test_span_never_negative_when_pickup_after_estimate(self):
        # Picked up so late it's past the (stale) estimate, still open: guard keeps
        # eff_end >= eff_start so the downstream pct/width math stays well-defined.
        span = _truthful_pill_span(
            sched_start_dt=_dt(13, 30), est_end_dt=_dt(14, 0),
            status='picked-up', trip_type='return',
            picked_up_dt=_dt(14, 10), completed_dt=None,
            now_dt=_dt(14, 15), is_today=True)
        self.assertGreaterEqual(span['eff_end'], span['eff_start'])

    # ── Pickup overdue: the pickup time passed with NO pickup recorded ───────
    def test_pickup_overdue_stalled_when_no_status(self):
        # The reported case: 4:30 pickup, now 4:36, driver shows nothing.
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 30), est_end_dt=_dt(17, 15),
            status='assigned', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(16, 36), is_today=True)
        self.assertTrue(span['pickup_overdue'])
        self.assertTrue(span['pickup_stalled'])       # no en-route/on-location report
        self.assertEqual(span['pickup_overdue_mins'], 6)

    def test_pickup_overdue_amber_when_driver_moving(self):
        # Past pickup but the driver IS reporting movement — watch, not alarm.
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 30), est_end_dt=_dt(17, 15),
            status='on-the-way', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(16, 40), is_today=True)
        self.assertTrue(span['pickup_overdue'])
        self.assertFalse(span['pickup_stalled'])

    def test_pickup_not_overdue_within_grace(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 30), est_end_dt=_dt(17, 15),
            status='assigned', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(16, 32), is_today=True)   # 2 min < 3 min grace
        self.assertFalse(span['pickup_overdue'])

    def test_pickup_overdue_clears_once_picked_up(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 30), est_end_dt=_dt(17, 15),
            status='picked-up', trip_type='return',
            picked_up_dt=_dt(16, 33), completed_dt=None,
            now_dt=_dt(16, 40), is_today=True)
        self.assertFalse(span['pickup_overdue'])

    def test_pickup_overdue_only_on_todays_board(self):
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 30), est_end_dt=_dt(17, 15),
            status='assigned', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(18, 0), is_today=False)
        self.assertFalse(span['pickup_overdue'])

    def test_stalled_pickup_does_not_draw_a_busy_overrun_bar(self):
        # Never picked up, no movement, and even the estimate has passed: this is a
        # STALLED pickup, not an overrun. The bar must not extend to now (which would
        # imply the driver is busy) — the pickup-overdue flag carries it instead.
        span = _truthful_pill_span(
            sched_start_dt=_dt(16, 30), est_end_dt=_dt(17, 15),
            status='assigned', trip_type='return',
            picked_up_dt=None, completed_dt=None,
            now_dt=_dt(17, 30), is_today=True)
        self.assertTrue(span['pickup_stalled'])
        self.assertFalse(span['overrunning'])
        self.assertEqual(span['eff_end'], _dt(17, 15))   # stays at the estimate


class PickupRiskUnifierTests(TestCase):
    """Unit tests for `_pickup_risk` — how the live-GPS band and the clock-based pickup
    flags combine into one escalating cue. GPS (proactive) outranks the clock fallback."""

    def _r(self, **kw):
        base = dict(pickup_overdue=False, pickup_stalled=False, overdue_mins=0,
                    gps_status='', gps_eta_mins=None, gps_reason='')
        base.update(kw)
        return _pickup_risk(**base)

    # ── GPS band drives it when telematics is fresh ──
    def test_gps_at_risk_is_critical_with_eta(self):
        r = self._r(gps_status='at_risk', gps_eta_mins=22,
                    gps_reason='ETA 22 min vs pickup in 15 min')
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['source'], 'gps')
        self.assertIn('22', r['label'])
        self.assertIn('ETA 22 min', r['reason'])

    def test_gps_late_is_critical(self):
        r = self._r(gps_status='late', gps_reason='Past pickup by 4 min')
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['source'], 'gps')

    def test_gps_watch_is_amber(self):
        r = self._r(gps_status='watch', gps_eta_mins=12,
                    gps_reason='Only 6 min slack to pickup')
        self.assertEqual(r['tier'], 'watch')
        self.assertEqual(r['source'], 'gps')

    def test_gps_not_moving_watch(self):
        # The "not on the way soon" case: sweep flags watch with a not-moving reason.
        r = self._r(gps_status='watch', gps_eta_mins=8,
                    gps_reason='Pickup in 20 min, vehicle not moving')
        self.assertEqual(r['tier'], 'watch')
        self.assertIn('not moving', r['reason'])

    def test_gps_on_time_is_no_risk(self):
        self.assertEqual(self._r(gps_status='on_time', gps_eta_mins=5)['tier'], '')

    def test_gps_unknown_is_no_risk(self):
        self.assertEqual(self._r(gps_status='unknown')['tier'], '')

    # ── Clock fallback when there's no fresh GPS ──
    def test_clock_stalled_is_critical(self):
        r = self._r(pickup_overdue=True, pickup_stalled=True, overdue_mins=6)
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['source'], 'clock')
        self.assertIn('6', r['label'])

    def test_clock_overdue_moving_is_amber(self):
        r = self._r(pickup_overdue=True, pickup_stalled=False, overdue_mins=6)
        self.assertEqual(r['tier'], 'watch')
        self.assertEqual(r['source'], 'clock')

    def test_nothing_is_no_risk(self):
        self.assertEqual(self._r()['tier'], '')

    # ── Precedence between sources ──
    def test_gps_at_risk_outranks_clock(self):
        # Both fire; the richer GPS reason wins the cue.
        r = self._r(gps_status='at_risk', gps_eta_mins=30, gps_reason='ETA 30 vs 10',
                    pickup_overdue=True, pickup_stalled=True, overdue_mins=5)
        self.assertEqual(r['source'], 'gps')

    def test_stalled_clock_outranks_gps_watch(self):
        # A stalled pickup (critical) beats a GPS 'watch' (amber).
        r = self._r(gps_status='watch', gps_eta_mins=9,
                    pickup_overdue=True, pickup_stalled=True, overdue_mins=7)
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['source'], 'clock')

    def test_at_risk_without_eta_falls_back_to_label(self):
        r = self._r(gps_status='at_risk', gps_eta_mins=None)
        self.assertEqual(r['tier'], 'critical')
        self.assertEqual(r['label'], 'at risk')


class TimelineRealityViewTest(TestCase):
    """Integration: a completed leg's ACTUAL clear time reaches the rendered slot."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        veh = Vehicle.objects.create(vehicle_type="towncar", capacity=4, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        route = Route.objects.create(origin=origin, destination=dest,
                                     inhouse_base_pay=Decimal("50.00"))
        rate = Rate.objects.create(vehicle=veh, route=route,
                                   oneway_price=Decimal("100.00"),
                                   round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone_number="5559990000")
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user("tr_sam", first_name="Sam"),
            driver_type="inhouse")
        fleet = FleetVehicle.objects.create(
            vehicle_number="T-9", vehicle_type=veh, year=2024, make="Lincoln",
            model="Continental")
        # Board is TODAY, so the "now"-relative rules are live.
        cls.today = timezone.localdate()
        DriverVehicleAssignment.objects.create(driver=cls.driver, date=cls.today, vehicle=fleet)

        res = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, rate=rate,
            vehicle=veh, base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        cls.leg = Leg.objects.create(
            reservation=res, pickup_date=cls.today, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", route=route,
            status="completed", driver=cls.driver)
        # Actual clear recorded well before the estimated clear would land.
        cleared_at = timezone.make_aware(datetime.combine(cls.today, time(9, 12)))
        LegStatus.objects.create(leg=cls.leg, status="completed", timestamp=cleared_at)

        cls.staff = User.objects.create_user("tr_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _find_slot(self, resp):
        for row in resp.context["inhouse_timeline"]:
            for slot in row["schedule"].slots:
                if slot.leg_id == self.leg.id:
                    return slot
        return None

    def test_completed_slot_reports_actual_clear(self):
        resp = self.client.get(
            reverse("schedule_board"), {"date": self.today.strftime("%Y-%m-%d")})
        self.assertEqual(resp.status_code, 200)
        slot = self._find_slot(resp)
        self.assertIsNotNone(slot, "completed leg should have a rendered slot")
        self.assertTrue(slot.cleared_is_actual)
        # The popup 'Clearing' read-out shows the actual time, no tilde, with meridiem.
        self.assertEqual(slot.end_time_display, "9:12 AM")
