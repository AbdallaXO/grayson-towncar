"""The Publix stop as a recorded event, and the live clearing time it produces.

Background — the bug this arc closes. `Reservation.store_stop` is a booking
checkbox, and it used to be the whole timing model: an arrival carrying it got a
flat 25 minutes bolted onto the DIRECT airport→destination drive, forever. So
between "Picked Up" and "Complete" the board had no idea whether a van was in a
grocery car park or already at the resort, and it printed a guess as a fact. That
is what raised a CRITICAL driver-conflict task against a driver who had tapped
picked-up at 1:27 PM but was still being priced as busy until 2:55 PM.

Three groups of tests here:
  * the timing model — segmented routing, and each driver tap sharpening the clear;
  * the invariants that must not be "improved" away (Leg.status untouched, billing
    untouched, no N+1 on planning paths);
  * the end-to-end fake task, with a control proving the test can still fail.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone as django_timezone

from dispatching import store_stop as ss
from dispatching.scheduler import (
    DRIVE_TIME_ESTIMATES, chain_clear_dt_from_actual,
)
from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, LegStatus, Reservation

User = get_user_model()

MCO = "Orlando International Airport (MCO)"
RESORT = "Disney's Polynesian Village Resort"
HOME = "1234 Maple St, Kissimmee"

PU_CAT, DO_CAT = "MCO Terminal", "Disney Resort"


def _dt(h, m):
    return datetime(2026, 8, 12, h, m)


# ═══════════════════════════════════════════════════════════════════════════
# The timing model (pure — no DB)
# ═══════════════════════════════════════════════════════════════════════════

class SegmentedRoutingTests(TestCase):
    """Airport → Publix → resort is a different road than airport → resort."""

    def test_store_leg_is_priced_as_the_route_actually_driven(self):
        direct = DRIVE_TIME_ESTIMATES[(PU_CAT, DO_CAT)]
        to_store = DRIVE_TIME_ESTIMATES[(PU_CAT, ss.PUBLIX_CATEGORY)]
        from_store = DRIVE_TIME_ESTIMATES[(ss.PUBLIX_CATEGORY, DO_CAT)]
        got = ss.in_job_minutes(PU_CAT, DO_CAT, ss.StoreStopState(expected=True))
        self.assertEqual(got, to_store + ss.PUBLIX_DWELL_MINUTES + from_store)
        # And it is NOT the old model, which drove straight there and added 25.
        self.assertNotEqual(got, direct + ss.LEGACY_FLAT_MINUTES)

    def test_a_leg_with_no_store_is_untouched(self):
        """Regression guard: the overwhelming majority of legs must not move."""
        direct = DRIVE_TIME_ESTIMATES[(PU_CAT, DO_CAT)]
        self.assertEqual(ss.in_job_minutes(PU_CAT, DO_CAT, None), direct)
        self.assertEqual(ss.in_job_minutes(PU_CAT, DO_CAT, ss.NO_STORE), direct)
        self.assertEqual(ss.detour_minutes(PU_CAT, DO_CAT, ss.NO_STORE), 0)

    def test_unpriceable_pair_degrades_to_the_shipped_flat_25(self):
        """A category pair the Publix table can't reach must fall back to the
        behaviour that shipped, not to a confident wrong answer."""
        got = ss.detour_minutes("Port Canaveral Area", "Other",
                                ss.StoreStopState(expected=True))
        self.assertEqual(got, ss.LEGACY_FLAT_MINUTES)

    def test_a_stop_can_never_cost_less_than_the_time_inside(self):
        """The floor. A thin table entry making a detour look free would hand the
        board slack it does not have — the same failure as the flat constant, from
        the optimistic side."""
        for pu in ("MCO Terminal", "SFB Terminal", "Universal Resort"):
            for do in ("Disney Resort", "Universal Resort", "Airport Hotel",
                       "Other Hotel", "Residential"):
                detour = ss.detour_minutes(pu, do, ss.StoreStopState(expected=True))
                self.assertGreaterEqual(
                    detour, ss.PUBLIX_DWELL_MINUTES,
                    f"{pu}->{do} priced a store stop at {detour} min")


class LiveClearingTests(TestCase):
    """Every driver tap retires a guess, so the clear time gets sharper."""

    def setUp(self):
        self.picked = _dt(13, 27)          # roberto's real tap

    def _clear(self, state):
        return ss.clear_dt_from_pickup(PU_CAT, DO_CAT, self.picked, state)

    def test_no_taps_yet_projects_the_whole_remaining_trip(self):
        dt, basis = self._clear(ss.StoreStopState(expected=True))
        self.assertEqual(basis, "projected")
        self.assertEqual(
            dt, self.picked + timedelta(
                minutes=ss.in_job_minutes(PU_CAT, DO_CAT,
                                          ss.StoreStopState(expected=True))))

    def test_arriving_at_the_store_re_anchors_on_that_moment(self):
        arrived = _dt(13, 52)
        dt, basis = self._clear(ss.StoreStopState(expected=True, arrived_at=arrived))
        self.assertEqual(basis, "at-store")
        # The detour is spent; only shopping + the last drive remain.
        self.assertEqual(dt, arrived + timedelta(
            minutes=ss.PUBLIX_DWELL_MINUTES
            + DRIVE_TIME_ESTIMATES[(ss.PUBLIX_CATEGORY, DO_CAT)]))

    def test_leaving_the_store_leaves_only_the_final_drive(self):
        left = _dt(14, 6)
        dt, basis = self._clear(ss.StoreStopState(
            expected=True, arrived_at=_dt(13, 52), departed_at=left))
        self.assertEqual(basis, "left-store")
        self.assertEqual(dt, left + timedelta(
            minutes=DRIVE_TIME_ESTIMATES[(ss.PUBLIX_CATEGORY, DO_CAT)]))

    def test_a_skipped_stop_removes_the_store_from_the_math_entirely(self):
        """The founder's guests skip the stop. A booking checkbox is not evidence
        about a trip already under way."""
        dt, basis = self._clear(ss.StoreStopState(expected=True, skipped=True))
        self.assertEqual(basis, "no-store")
        self.assertEqual(dt, self.picked + timedelta(
            minutes=DRIVE_TIME_ESTIMATES[(PU_CAT, DO_CAT)]))

    def test_a_stop_added_last_minute_is_priced_even_though_nobody_booked_it(self):
        """The other half of the founder's rule: guests add it when we have slack."""
        state = ss.StoreStopState(expected=False, arrived_at=_dt(13, 52))
        self.assertTrue(state.adhoc)
        self.assertTrue(state.happening)
        dt, basis = self._clear(state)
        self.assertEqual(basis, "at-store")
        self.assertGreater(dt, self.picked + timedelta(
            minutes=DRIVE_TIME_ESTIMATES[(PU_CAT, DO_CAT)]))

    def test_shopping_time_is_measured_once_both_taps_exist(self):
        state = ss.StoreStopState(expected=True, arrived_at=_dt(13, 52),
                                  departed_at=_dt(14, 6))
        self.assertEqual(state.shopped_minutes, 14)
        self.assertIsNone(
            ss.StoreStopState(expected=True, arrived_at=_dt(13, 52)).shopped_minutes)


# ═══════════════════════════════════════════════════════════════════════════
# Shared DB fixture
# ═══════════════════════════════════════════════════════════════════════════

class StoreStopDBCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(
            first_name="Store", last_name="Case",
            email="store@example.com", phone_number="4070000009",
        )
        route = Route.objects.create(
            origin=Location.objects.create(name="Orlando International Airport"),
            destination=Location.objects.create(name="Disney Property"),
        )
        vehicle = Vehicle.objects.create(vehicle_type="van", capacity=14,
                                         luggage_capacity=14)
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("120"), round_trip_price=Decimal("220"),
        )
        cls.day = date(2026, 8, 12)
        cls.user = User.objects.create_user(username="roberto-t", password="x",
                                            first_name="roberto")
        cls.driver = Driver.objects.create(profile=cls.user, driver_type="inhouse")

    def _res(self, store_stop=True):
        return Reservation.objects.create(
            customer=self.customer, rate=self.rate, trip_type="one_way",
            base_price=Decimal("100"), total_price=Decimal("100"),
            store_stop=store_stop,
        )

    def _leg(self, res, *, pickup=MCO, dropoff=RESORT, at=time(12, 59),
             status="picked-up", driver=None):
        return Leg.objects.create(
            reservation=res, pickup_date=self.day, pickup_time=at,
            pickup_location=pickup, dropoff_location=dropoff,
            status=status, driver=driver,
        )

    def _tap(self, leg, status, h, m):
        stamp = django_timezone.make_aware(datetime(2026, 8, 12, h, m))
        return LegStatus.objects.create(leg=leg, status=status, timestamp=stamp,
                                        updated_by=self.user)


# ═══════════════════════════════════════════════════════════════════════════
# State resolution
# ═══════════════════════════════════════════════════════════════════════════

class ResolveStoreStateTests(StoreStopDBCase):
    def test_taps_are_read_off_the_status_trail(self):
        leg = self._leg(self._res())
        self._tap(leg, "picked-up", 13, 27)
        self.assertEqual(ss.resolve_store_state(leg).phase, "expected")
        self._tap(leg, ss.STATUS_ARRIVED, 13, 52)
        self.assertEqual(ss.resolve_store_state(leg).phase, "shopping")
        self._tap(leg, ss.STATUS_DEPARTED, 14, 6)
        state = ss.resolve_store_state(leg)
        self.assertEqual(state.phase, "rolling")
        self.assertEqual(state.shopped_minutes, 14)

    def test_earliest_tap_wins_whichever_way_the_rows_are_ordered(self):
        """Prefetched paths hand rows newest-first, others oldest-first. The
        resolved state must not depend on which."""
        leg = self._leg(self._res())
        self._tap(leg, ss.STATUS_ARRIVED, 13, 52)
        self._tap(leg, ss.STATUS_ARRIVED, 14, 1)      # double tap
        oldest = list(leg.status_history.order_by("timestamp"))
        newest = list(leg.status_history.order_by("-timestamp"))
        a = ss.resolve_store_state(leg, status_rows=oldest)
        b = ss.resolve_store_state(leg, status_rows=newest)
        self.assertEqual(a.arrived_at, b.arrived_at)
        self.assertEqual(a.arrived_at.hour, 13)
        self.assertEqual(a.arrived_at.minute, 52)

    def test_the_guest_changing_their_mind_after_a_skip_still_records(self):
        leg = self._leg(self._res())
        self._tap(leg, ss.STATUS_SKIPPED, 13, 30)
        self.assertEqual(ss.resolve_store_state(leg).phase, "skipped")
        self._tap(leg, ss.STATUS_ARRIVED, 13, 52)
        state = ss.resolve_store_state(leg)
        self.assertFalse(state.skipped)
        self.assertEqual(state.phase, "shopping")

    def test_the_flag_only_counts_on_the_leg_the_grocery_run_rides(self):
        """`shows_store_stop`, not the raw reservation flag — otherwise the
        guest's departure leg back to MCO gets priced for shopping too."""
        res = self._res()
        arrival = self._leg(res, status="confirmed")
        departure = self._leg(res, pickup=RESORT, dropoff=MCO, at=time(9, 0),
                              status="confirmed")
        self.assertTrue(ss.resolve_store_state(arrival).expected)
        self.assertFalse(ss.resolve_store_state(departure).expected)

    def test_a_leg_not_yet_under_way_never_reads_the_status_trail(self):
        """Query budget. chain_clear_dt runs this over every leg on the board
        during auto-assign, and status_history is NOT prefetched there — a read
        per leg would be an N+1 on the hot path. A leg that hasn't reached
        picked-up cannot have taps, so it is answered from the booking flag
        alone. (The reservation itself is select_related on every real caller,
        which is why it's joined here rather than counted.)"""
        leg = self._leg(self._res(), status="confirmed")
        leg = Leg.objects.select_related("reservation").get(pk=leg.pk)
        with self.assertNumQueries(0):
            state = ss.resolve_store_state(leg)
        self.assertEqual(state.phase, "expected")


# ═══════════════════════════════════════════════════════════════════════════
# Invariants — do not "improve" these away
# ═══════════════════════════════════════════════════════════════════════════

class StoreStopInvariantTests(StoreStopDBCase):
    def setUp(self):
        self.client.force_login(self.user)
        self.res = self._res()
        self.leg = self._leg(self.res, driver=self.driver)
        self._tap(self.leg, "picked-up", 13, 27)

    def _post(self, event, leg=None):
        return self.client.post(
            f"/drivers/update_leg_store_stop/{(leg or self.leg).id}/",
            data={"event": event}, content_type="application/json",
        )

    def test_a_store_tap_never_becomes_the_leg_status(self):
        """~60 files filter, colour and count off Leg.status. A guest standing in
        an aisle is still picked up by every one of those measures."""
        self.assertEqual(self._post(ss.STATUS_ARRIVED).status_code, 200)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, "picked-up")
        self.assertEqual(self._post(ss.STATUS_DEPARTED).status_code, 200)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, "picked-up")

    def test_a_driver_tap_never_changes_what_the_guest_was_quoted(self):
        """An ad-hoc stop is an operational fact, not a billing decision."""
        res = self._res(store_stop=False)
        leg = self._leg(res, driver=self.driver)
        self._tap(leg, "picked-up", 13, 27)
        self.assertEqual(self._post(ss.STATUS_ARRIVED, leg=leg).status_code, 200)
        res.refresh_from_db()
        self.assertFalse(res.store_stop)          # untouched
        state = ss.resolve_store_state(leg)
        self.assertTrue(state.adhoc)              # but dispatch can see it

    def test_the_stop_cannot_be_recorded_before_the_guest_is_aboard(self):
        leg = self._leg(self._res(), driver=self.driver, status="on-the-way")
        self.assertEqual(self._post(ss.STATUS_ARRIVED, leg=leg).status_code, 409)

    def test_leaving_before_arriving_is_refused(self):
        self.assertEqual(self._post(ss.STATUS_DEPARTED).status_code, 409)

    def test_an_unknown_event_is_refused(self):
        self.assertEqual(self._post("store-exploded").status_code, 400)

    def test_a_driver_cannot_record_a_stop_on_somebody_elses_trip(self):
        other_user = User.objects.create_user(username="other-d", password="x",
                                              first_name="Other")
        other = Driver.objects.create(profile=other_user, driver_type="inhouse")
        leg = self._leg(self._res(), driver=other)
        self._tap(leg, "picked-up", 13, 27)
        self.assertEqual(self._post(ss.STATUS_ARRIVED, leg=leg).status_code, 404)


# ═══════════════════════════════════════════════════════════════════════════
# The driver's screen
# ═══════════════════════════════════════════════════════════════════════════

class DriverPortalRenderTests(StoreStopDBCase):
    """The buttons have to actually be on the page a driver holds."""

    def setUp(self):
        self.client.force_login(self.user)
        self.res = self._res()
        self.leg = self._leg(self.res, driver=self.driver)

    def _page(self):
        return self.client.get(f"/drivers/?date={self.day.isoformat()}").content.decode()

    def test_a_picked_up_store_leg_offers_both_the_stop_and_the_skip(self):
        self._tap(self.leg, "picked-up", 13, 27)
        html = self._page()
        self.assertIn("At Publix", html)
        self.assertIn("Skipping the stop", html)
        self.assertIn(ss.STATUS_ARRIVED, html)

    def test_the_next_tap_is_the_only_one_offered(self):
        self._tap(self.leg, "picked-up", 13, 27)
        self._tap(self.leg, ss.STATUS_ARRIVED, 13, 52)
        html = self._page()
        self.assertIn("Leaving Publix", html)
        self.assertIn("At Publix since", html)
        self.assertNotIn("Skipping the stop", html)

    def test_a_leg_nobody_booked_a_stop_for_can_still_add_one(self):
        """Guests add it last-minute when we have the slack, so the control is
        offered on every picked-up leg, not just the flagged ones."""
        res = self._res(store_stop=False)
        leg = self._leg(res, driver=self.driver, at=time(9, 15))
        self._tap(leg, "picked-up", 9, 30)
        self.assertIn("Stopping at Publix", self._page())

    def test_nothing_is_offered_before_the_guest_is_aboard(self):
        self.leg.status = "on-the-way"
        self.leg.save(update_fields=["status"])
        html = self._page()
        self.assertNotIn(ss.STATUS_ARRIVED, html)


# ═══════════════════════════════════════════════════════════════════════════
# The fake task, end to end
# ═══════════════════════════════════════════════════════════════════════════

class FakeConflictTaskTests(StoreStopDBCase):
    """The reported case: roberto tapped Picked Up at 1:27 PM on a 12:59 arrival
    with a Publix stop, and the scanner still priced him busy until 2:55 — then
    raised a CRITICAL conflict about his next airport pickup."""

    def _prior_and_next(self):
        prior = self._leg(self._res(), driver=self.driver, at=time(12, 59))
        nxt = self._leg(self._res(store_stop=False), driver=self.driver,
                        pickup=MCO, dropoff=HOME, at=time(15, 8),
                        status="confirmed")
        return prior, nxt

    def test_a_recorded_pickup_moves_the_clear_time_off_the_flight_clock(self):
        from ops.tasks import _estimate_leg_end_time

        prior, _ = self._prior_and_next()
        prior = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
        predicted = _estimate_leg_end_time(prior, self.day)

        self._tap(prior, "picked-up", 13, 27)
        prior = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
        live = _estimate_leg_end_time(prior, self.day)

        self.assertLess(live, predicted,
                        "a driver who picked up early must clear earlier")
        self.assertEqual(live, chain_clear_dt_from_actual(
            prior, _dt(13, 27), store_state=ss.resolve_store_state(prior)))

    def test_each_store_tap_re_anchors_the_clear_on_a_measured_moment(self):
        """The point is accuracy, not optimism. A tap can move the clear time
        LATER — a driver who took 25 minutes to reach a store the model gives 20
        really will finish later, and the board should say so."""
        from ops.tasks import _estimate_leg_end_time

        prior, _ = self._prior_and_next()
        self._tap(prior, "picked-up", 13, 27)

        def clear():
            fresh = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
            return _estimate_leg_end_time(fresh, self.day)

        projected = clear()
        self._tap(prior, ss.STATUS_ARRIVED, 13, 52)     # 25 min to the store
        at_store = clear()
        self._tap(prior, ss.STATUS_DEPARTED, 14, 6)     # shopped 14, model says 18
        rolling = clear()

        from_store = DRIVE_TIME_ESTIMATES[(ss.PUBLIX_CATEGORY, DO_CAT)]
        # Once he is inside, the clear hangs off the arrival tap.
        self.assertEqual(at_store, _dt(13, 52) + timedelta(
            minutes=ss.PUBLIX_DWELL_MINUTES + from_store))
        # Once he is out, it hangs off the departure tap and nothing else.
        self.assertEqual(rolling, _dt(14, 6) + timedelta(minutes=from_store))
        # He shopped faster than the model, so leaving beats standing inside.
        self.assertLess(rolling, at_store)
        # ...but he was slow reaching the store, so he clears slightly after the
        # pickup-only projection. Reality is allowed to be worse than the plan.
        self.assertGreater(rolling, projected)

    def test_a_fast_trip_pulls_the_clear_time_in(self):
        from ops.tasks import _estimate_leg_end_time

        prior, _ = self._prior_and_next()
        self._tap(prior, "picked-up", 13, 27)
        fresh = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
        projected = _estimate_leg_end_time(fresh, self.day)

        self._tap(prior, ss.STATUS_ARRIVED, 13, 40)     # quick to the store
        self._tap(prior, ss.STATUS_DEPARTED, 13, 48)    # and a quick shop
        fresh = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
        self.assertLess(_estimate_leg_end_time(fresh, self.day), projected)

    def test_skipping_the_stop_frees_the_driver_sooner(self):
        from ops.tasks import _estimate_leg_end_time

        prior, _ = self._prior_and_next()
        self._tap(prior, "picked-up", 13, 27)
        fresh = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
        with_stop = _estimate_leg_end_time(fresh, self.day)

        self._tap(prior, ss.STATUS_SKIPPED, 13, 30)
        fresh = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
        without = _estimate_leg_end_time(fresh, self.day)

        self.assertLess(without, with_stop)
        self.assertEqual(
            without,
            _dt(13, 27) + timedelta(minutes=DRIVE_TIME_ESTIMATES[(PU_CAT, DO_CAT)]))

    def test_a_leg_with_no_recorded_pickup_still_uses_the_prediction(self):
        """The control. Detection reads reality only where reality exists — a leg
        nobody has touched must keep the flight-anchored forecast, or this whole
        change would just be blanket optimism."""
        from ops.tasks import _estimate_leg_end_time
        from dispatching.scheduler import estimate_job_end_time

        prior, _ = self._prior_and_next()
        prior.status = "confirmed"
        prior.save(update_fields=["status"])
        fresh = Leg.objects.prefetch_related("status_history").get(pk=prior.pk)
        self.assertEqual(_estimate_leg_end_time(fresh, self.day),
                         estimate_job_end_time(fresh, self.day))
