"""Tests for the early-flight "tight turn" safety net.

Run with:  ./manage.py test ops.tests.test_tight_turn

Covers:
  * classify_turn() tier thresholds — the founder's rule (driver arrival vs the RAW
    flight arrival, no deplaning padding): >=15 min after → red "won't make it",
    0..15 min after → amber "tight", before the flight → no flag.
  * Leg.flight_timing_flag() board signal — amber 'watch' for early arrivals
    (15..19 min), red 'alert' at >= 20 min either direction.
  * _scan_driver_overlaps() raising the right task type, and escalation amber→red
    closing the softer flag.
"""
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg, Flight, LegKeoi
from ops.models import OperationalTask
from ops.tasks import (
    classify_turn, detect_driver_conflicts, _scan_driver_overlaps,
    _auto_close_resolved_tasks, reconcile_conflict_keois,
)
from ops.services import create_task
from drivers.models import Driver

TARGET = date(2026, 6, 1)

# Saving a Leg (which these tests do constantly, to fire the reassignment signal)
# kicks off side-effect work in background daemon threads via
# reservations.utils._run_in_background. On SQLite those race the test's own
# writes — 216 "database table is locked" lines in one run of this module. None
# of it matters to conflict-flag correctness, so neutralise the spawns module-wide
# at every binding site, exactly as dispatching/tests_keoi.py does.
_NOOP = lambda *a, **k: None
_bg_targets = [
    "reservations.utils._run_in_background",   # source (function-local imports)
    "drivers.signals._run_in_background",      # module-level bind (driver notifications)
    "dispatching.views._run_in_background",    # module-level bind (board endpoints)
]
_bg_patchers = []


def setUpModule():
    for target in _bg_targets:
        p = patch(target, _NOOP)
        p.start()
        _bg_patchers.append(p)


def tearDownModule():
    for p in _bg_patchers:
        p.stop()
    _bg_patchers.clear()


class _TurnFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone_number="5559876543",
        )
        user = User.objects.create_user(username="tt_driver", first_name="Tito")
        cls.driver = Driver.objects.create(profile=user, driver_type="inhouse")

    def _res(self):
        return Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )

    def _leg(self, pickup, dropoff, pickup_time, **kw):
        defaults = dict(
            reservation=self._res(), pickup_date=TARGET, pickup_time=pickup_time,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route,
            status="confirmed", driver=self.driver,
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _arrival_leg_with_flight(self, pickup_time, arrival_dt):
        """An MCO→Disney arrival leg with a flight whose best arrival is arrival_dt."""
        flight = Flight.objects.create(
            airline="DL", flight_number="100",
            scheduled_arrival_local=arrival_dt,
        )
        leg = self._leg("MCO Airport", "Disney Resort", pickup_time)
        leg.flight_information = flight
        leg.save()
        return leg


class ClassifyTurnTierTests(_TurnFixtureMixin, TestCase):
    """The founder's example: a 9:00 Disney→MCO return then a 9:30 MCO→Disney
    arrival, same driver, who is free at the airport at ~9:30. The arrival flight
    creeps earlier."""

    def setUp(self):
        self.prior = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        self.arrival = self._arrival_leg_with_flight(
            time(9, 30), datetime(2026, 6, 1, 9, 30)
        )

    def _tier(self, raw_arrival, driver_free):
        with patch("ops.tasks._get_raw_arrival_dt", return_value=raw_arrival), \
             patch("ops.tasks._estimate_leg_end_time", return_value=driver_free), \
             patch("ops.tasks._reposition_minutes", return_value=0):
            return classify_turn(self.prior, self.arrival, TARGET)

    def test_flight_well_before_driver_is_red(self):
        free = datetime(2026, 6, 1, 9, 30)
        # 9:00 → 30 min after; 9:10 → 20 min after — both "won't make it".
        for raw in (datetime(2026, 6, 1, 9, 0), datetime(2026, 6, 1, 9, 10)):
            risk = self._tier(raw, free)
            self.assertIsNotNone(risk)
            self.assertEqual(risk["tier"], "red")

    def test_fifteen_min_after_is_flagged_red(self):
        # Founder: "even 15 minutes after arrival, still flag that."
        risk = self._tier(datetime(2026, 6, 1, 9, 15), datetime(2026, 6, 1, 9, 30))
        self.assertIsNotNone(risk)
        self.assertEqual(risk["tier"], "red")
        self.assertEqual(risk["late"], 15)

    def test_ten_min_after_is_amber(self):
        # 9:20 flight, driver there 9:30 → 10 min after → "keep an eye".
        risk = self._tier(datetime(2026, 6, 1, 9, 20), datetime(2026, 6, 1, 9, 30))
        self.assertIsNotNone(risk)
        self.assertEqual(risk["tier"], "amber")
        self.assertEqual(risk["late"], 10)

    def test_driver_there_before_flight_is_no_flag(self):
        # Driver free 9:30, flight lands 9:50 → he's there 20 min early.
        self.assertIsNone(self._tier(datetime(2026, 6, 1, 9, 50), datetime(2026, 6, 1, 9, 30)))
        # Exactly on the dot (9:30) is fine too.
        self.assertIsNone(self._tier(datetime(2026, 6, 1, 9, 30), datetime(2026, 6, 1, 9, 30)))


class FlightTimingFlagTests(_TurnFixtureMixin, TestCase):
    """Board badge: amber 'watch' for early 15..19, red 'alert' at >= 20 either way."""

    def _flag(self, minutes_delta):
        """minutes_delta < 0 = flight earlier than pickup."""
        pickup = time(9, 30)
        arr = datetime(2026, 6, 1, 9, 30) + timezone.timedelta(minutes=minutes_delta)
        leg = self._arrival_leg_with_flight(pickup, arr)
        return leg.flight_timing_flag()

    def test_early_15_is_watch(self):
        flag = self._flag(-15)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["level"], "watch")
        self.assertEqual(flag["direction"], "early")

    def test_early_22_is_alert(self):
        flag = self._flag(-22)
        self.assertEqual(flag["level"], "alert")

    def test_late_25_is_alert(self):
        flag = self._flag(25)
        self.assertEqual(flag["level"], "alert")
        self.assertEqual(flag["direction"], "late")

    def test_early_4_is_none(self):
        self.assertIsNone(self._flag(-4))

    def test_late_10_is_none(self):
        # Late but under the 20-min alert and there's no late 'watch' tier.
        self.assertIsNone(self._flag(10))


class DriverOverlapScanTests(_TurnFixtureMixin, TestCase):
    """The scan creates the right task type and escalation closes the soft flag."""

    def setUp(self):
        self.prior = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        self.arrival = self._arrival_leg_with_flight(
            time(9, 30), datetime(2026, 6, 1, 9, 30)
        )
        self.fixed_now = timezone.make_aware(datetime(2026, 6, 1, 6, 0))

    def _run_scan(self, raw_arrival, driver_free=datetime(2026, 6, 1, 9, 30)):
        with patch("ops.tasks.timezone.now", return_value=self.fixed_now), \
             patch("ops.tasks.timezone.localdate", return_value=TARGET), \
             patch("ops.tasks._reposition_minutes", return_value=0), \
             patch("ops.tasks._estimate_leg_end_time", return_value=driver_free), \
             patch("ops.tasks._get_raw_arrival_dt", return_value=raw_arrival):
            return _scan_driver_overlaps()

    def _open(self, task_type):
        return OperationalTask.objects.filter(
            task_type=task_type,
            leg=self.arrival,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )

    def test_amber_creates_tight_turn_task(self):
        self._run_scan(datetime(2026, 6, 1, 9, 20))  # 10 min after → amber
        self.assertTrue(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())
        self.assertFalse(self._open(OperationalTask.TaskType.DRIVER_CONFLICT).exists())

    def test_red_creates_driver_conflict_task(self):
        self._run_scan(datetime(2026, 6, 1, 9, 0))  # 30 min after → red
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT).exists())
        self.assertFalse(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())

    def test_escalation_closes_open_tight_turn(self):
        # Pre-existing amber flag for the leg…
        create_task(
            task_type=OperationalTask.TaskType.TIGHT_TURN,
            title="Tight turn — Tito",
            leg=self.arrival,
            reservation=self.arrival.reservation,
        )
        self.assertTrue(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())
        # …a now-earlier flight escalates it to red.
        self._run_scan(datetime(2026, 6, 1, 9, 0))
        self.assertFalse(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT).exists())


class ClassifyTurnBookedPickupTierTests(_TurnFixtureMixin, TestCase):
    """The booked-pickup-to-booked-pickup shape — next leg is NOT a tracked flight
    arrival (e.g. an airport drop-off followed by a hotel pickup). This branch used
    to have no amber tier at all: any late minute, however small, was hardcoded to
    red. That's the exact alert-fatigue bug — a driver 0-6 min "behind" showing up
    identical to one who's an hour late. Shares TIGHT_TURN_RED_AFTER_MIN (10) with
    the flight-arrival branch above — one clock for both turn shapes."""

    def setUp(self):
        self.prior = self._leg("Disney's Grand Floridian Resort", "MCO Airport", time(15, 30))
        self.next_pickup = self._leg("Loews Royal Pacific Resort", "MCO Airport", time(16, 30))

    def _tier(self, driver_free):
        with patch("ops.tasks._estimate_leg_end_time", return_value=driver_free), \
             patch("ops.tasks._reposition_minutes", return_value=0):
            return classify_turn(self.prior, self.next_pickup, TARGET)

    def test_on_time_or_early_is_no_flag(self):
        self.assertIsNone(self._tier(datetime(2026, 6, 1, 16, 30)))
        self.assertIsNone(self._tier(datetime(2026, 6, 1, 16, 20)))

    def test_one_to_ten_min_behind_is_amber(self):
        # The screenshot cases (0 and 6 min "behind pickup"): a driver clearing
        # the prior job within TIGHT_TURN_RED_AFTER_MIN of the next ready time is
        # "keep an eye on it", not a CRITICAL emergency.
        for late in (1, 6, 10):
            risk = self._tier(datetime(2026, 6, 1, 16, 30) + timezone.timedelta(minutes=late))
            self.assertIsNotNone(risk, f"{late} min behind should still flag")
            self.assertEqual(risk["tier"], "amber")
            self.assertEqual(risk["late"], late)

    def test_over_ten_min_behind_is_red(self):
        risk = self._tier(datetime(2026, 6, 1, 16, 41))  # 11 min behind
        self.assertIsNotNone(risk)
        self.assertEqual(risk["tier"], "red")
        self.assertEqual(risk["late"], 11)


class DetectDriverConflictsTierTests(_TurnFixtureMixin, TestCase):
    """detect_driver_conflicts (used by the flight-shift-triggered same-day/future
    scanners) must band the same way classify_turn does, not just flag on any
    conflict_minutes > 0."""

    def setUp(self):
        self.checked = self._leg("Disney's Grand Floridian Resort", "MCO Airport", time(15, 30))
        self.other = self._leg("Loews Royal Pacific Resort", "MCO Airport", time(16, 30))

    def _conflicts(self, checked_end):
        with patch("ops.tasks._estimate_leg_end_time", side_effect=lambda leg, d: (
            checked_end if leg.pk == self.checked.pk else datetime(2026, 6, 1, 20, 0)
        )), patch("ops.tasks._reposition_minutes", return_value=0):
            return detect_driver_conflicts(self.checked, TARGET)

    def test_six_min_late_is_amber_not_hardcoded_red(self):
        conflicts = self._conflicts(datetime(2026, 6, 1, 16, 36))  # 6 min late to `other`
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["tier"], "amber")
        self.assertEqual(conflicts[0]["conflict_minutes"], 6)

    def test_thirty_min_late_is_red(self):
        conflicts = self._conflicts(datetime(2026, 6, 1, 17, 0))  # 30 min late
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["tier"], "red")


class DriverConflictKeoiTests(_TurnFixtureMixin, TestCase):
    """A genuinely red conflict raises the board's watch-flag (KEOI) on the
    affected leg — the "turn the next one into a KEOI" behavior — while an amber
    (tight-but-makes-it) turn must not."""

    def setUp(self):
        self.prior = self._leg("Disney's Grand Floridian Resort", "MCO Airport", time(15, 30))
        self.next_pickup = self._leg("Loews Royal Pacific Resort", "MCO Airport", time(16, 30))
        self.fixed_now = timezone.make_aware(datetime(2026, 6, 1, 6, 0))

    def _run_scan(self, driver_free):
        with patch("ops.tasks.timezone.now", return_value=self.fixed_now), \
             patch("ops.tasks.timezone.localdate", return_value=TARGET), \
             patch("ops.tasks._reposition_minutes", return_value=0), \
             patch("ops.tasks._estimate_leg_end_time", return_value=driver_free):
            return _scan_driver_overlaps()

    def _open_keoi(self):
        return LegKeoi.objects.filter(leg=self.next_pickup, closed_at__isnull=True)

    def test_red_conflict_raises_keoi(self):
        self._run_scan(datetime(2026, 6, 1, 16, 50))  # 20 min late → red
        self.assertTrue(self._open_keoi().exists())
        self.assertEqual(self._open_keoi().first().category, LegKeoi.Category.DRIVER_CONFLICT)

    def test_amber_turn_does_not_raise_keoi(self):
        self._run_scan(datetime(2026, 6, 1, 16, 36))  # 6 min late → amber
        self.assertFalse(self._open_keoi().exists())

    def test_duplicate_keoi_not_created_if_already_open(self):
        LegKeoi.objects.create(
            leg=self.next_pickup, category=LegKeoi.Category.OTHER,
            description="Pre-existing flag", operational_status=LegKeoi.OperationalStatus.NEEDS_ATTENTION,
        )
        self._run_scan(datetime(2026, 6, 1, 16, 50))  # 20 min late → red
        self.assertEqual(self._open_keoi().count(), 1)


class ConflictKeoiTakedownTests(_TurnFixtureMixin, TestCase):
    """The board cried wolf: a red conflict raised a KEOI, the conflict resolved,
    the task auto-closed — and the flag stayed lit forever. These cover the
    takedown side, including flags orphaned by a path that never closed them."""

    def setUp(self):
        self.prior = self._leg("Disney's Grand Floridian Resort", "MCO Airport", time(15, 30))
        self.next_pickup = self._leg("Loews Royal Pacific Resort", "MCO Airport", time(16, 30))
        self.fixed_now = timezone.make_aware(datetime(2026, 6, 1, 6, 0))

    def _run_scan(self, driver_free):
        with patch("ops.tasks.timezone.now", return_value=self.fixed_now), \
             patch("ops.tasks.timezone.localdate", return_value=TARGET), \
             patch("ops.tasks._reposition_minutes", return_value=0), \
             patch("ops.tasks._estimate_leg_end_time", return_value=driver_free):
            return _scan_driver_overlaps()

    def _run_autoclose(self, driver_free=None):
        """driver_free pins the driver's clear time exactly as _run_scan does, so
        a conflict that was red at scan time is still red at re-check time.
        Omit it when the point of the test is that the conflict is gone."""
        stack = [
            patch("ops.tasks.timezone.now", return_value=self.fixed_now),
            patch("ops.tasks.timezone.localdate", return_value=TARGET),
            patch("ops.tasks._reposition_minutes", return_value=0),
        ]
        if driver_free is not None:
            stack.append(patch("ops.tasks._estimate_leg_end_time", return_value=driver_free))
        for p in stack:
            p.start()
        try:
            return _auto_close_resolved_tasks()
        finally:
            for p in reversed(stack):
                p.stop()

    def _open_keoi(self):
        return LegKeoi.objects.filter(leg=self.next_pickup, closed_at__isnull=True)

    def test_flag_comes_down_when_the_conflict_resolves(self):
        self._run_scan(datetime(2026, 6, 1, 16, 50))  # 20 min late → red, flag up
        self.assertTrue(self._open_keoi().exists())

        # Dispatcher gives the tight leg to someone else — conflict is gone.
        other_user = User.objects.create_user(username="tt_driver2", first_name="Rey")
        self.prior.driver = Driver.objects.create(profile=other_user, driver_type="inhouse")
        self.prior.save()

        self._run_autoclose()
        self.assertFalse(self._open_keoi().exists())
        self.assertEqual(
            self._open_keoi().model.objects.filter(leg=self.next_pickup).first().closed_reason,
            LegKeoi.ClosedReason.CONFLICT_RESOLVED,
        )

    def test_orphaned_flag_is_swept_even_with_no_task_behind_it(self):
        """The board's actual failure: a flag whose task was closed by some other
        path (or never existed) had nothing to take it down."""
        LegKeoi.objects.create(
            leg=self.next_pickup, category=LegKeoi.Category.DRIVER_CONFLICT,
            description="stale — driver 30 min late", created_by=None,
            operational_status=LegKeoi.OperationalStatus.NEEDS_ATTENTION,
        )
        self.assertTrue(self._open_keoi().exists())
        self._run_autoclose()
        self.assertFalse(self._open_keoi().exists())

    def test_reassigning_the_driver_drops_the_flag_immediately(self):
        """The gap the 30-min sweep left: a dispatcher reassigns the tight leg and
        the board must stop showing red on the NEXT render, not half an hour later.
        Observed live: leg 30493 carried a flag naming "Junaid Baidr" while the leg
        already belonged to "JoseT"."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))  # 20 min late → red, flag up
        self.assertTrue(self._open_keoi().exists())

        # Dispatcher hands the tight leg to someone else. No scan runs.
        spare = User.objects.create_user(username="tt_spare", first_name="Nadia")
        self.next_pickup.driver = Driver.objects.create(profile=spare, driver_type="inhouse")
        self.next_pickup.save(update_fields=["driver"])

        self.assertFalse(
            self._open_keoi().exists(),
            "flag survived the reassignment — the board is still crying wolf",
        )

    def test_reassigning_the_OTHER_leg_drops_the_flag_immediately(self):
        """The flag sits on the leg the driver is late TO, but the conflict dies just
        as dead when the EARLIER leg is the one handed off. Reconciliation has to
        reach flags on legs other than the one being saved."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        self.assertTrue(self._open_keoi().exists())

        spare = User.objects.create_user(username="tt_spare2", first_name="Omar")
        self.prior.driver = Driver.objects.create(profile=spare, driver_type="inhouse")
        self.prior.save(update_fields=["driver"])

        self.assertFalse(
            self._open_keoi().exists(),
            "flag survived reassignment of the conflicting leg",
        )

    def test_unassigning_the_driver_drops_the_flag_immediately(self):
        """No driver, no conflict."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        self.assertTrue(self._open_keoi().exists())

        self.next_pickup.driver = None
        self.next_pickup.save(update_fields=["driver"])

        self.assertFalse(self._open_keoi().exists())

    def test_reassignment_does_not_touch_a_dispatcher_raised_flag(self):
        """Instant takedown must respect the same boundary the sweep does."""
        boss = User.objects.create_user(username="tt_boss")
        LegKeoi.objects.create(
            leg=self.prior, category=LegKeoi.Category.DRIVER_CONFLICT,
            description="my own eyes on this", created_by=boss,
            operational_status=LegKeoi.OperationalStatus.NEEDS_ATTENTION,
        )
        spare = User.objects.create_user(username="tt_spare3", first_name="Lena")
        self.prior.driver = Driver.objects.create(profile=spare, driver_type="inhouse")
        self.prior.save(update_fields=["driver"])

        self.assertTrue(
            LegKeoi.objects.filter(leg=self.prior, closed_at__isnull=True).exists()
        )

    def test_adopted_system_flag_survives_reassignment(self):
        """A dispatcher who ADOPTS a system flag — marks it Backup Arranged and
        writes who is covering — owns it from then on. created_by stays NULL on an
        adopted flag (only updated_by is set), so a created_by-only guard would
        close it and bin their note."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))  # 20 min late → red, flag up
        flag = self._open_keoi().first()
        self.assertIsNotNone(flag)

        boss = User.objects.create_user(username="tt_adopter")
        flag.operational_status = LegKeoi.OperationalStatus.BACKUP_ARRANGED
        flag.description = "Marcus is covering, confirmed by phone"
        flag.updated_by = boss
        flag.save(update_fields=["operational_status", "description", "updated_by"])

        spare = User.objects.create_user(username="tt_spare4", first_name="Ivy")
        self.next_pickup.driver = Driver.objects.create(profile=spare, driver_type="inhouse")
        self.next_pickup.save(update_fields=["driver"])

        still = self._open_keoi().first()
        self.assertIsNotNone(still, "reassignment binned a flag a dispatcher had adopted")
        self.assertEqual(still.description, "Marcus is covering, confirmed by phone")

    def test_adopted_system_flag_survives_the_sweep(self):
        """Same boundary, enforced on the 30-minute sweep as well as the signal."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        flag = self._open_keoi().first()
        boss = User.objects.create_user(username="tt_adopter2")
        flag.updated_by = boss
        flag.operational_status = LegKeoi.OperationalStatus.BEING_MONITORED
        flag.save(update_fields=["updated_by", "operational_status"])

        # Conflict evaporates, so the backing task closes and the flag is orphaned.
        self.next_pickup.driver = None
        self.next_pickup.save(update_fields=["driver"])
        self._run_autoclose()

        self.assertTrue(
            self._open_keoi().exists(),
            "the sweep binned a flag a dispatcher had adopted",
        )

    # ── Legacy task shape: no affected_leg_id, flag on the partner leg ───────
    # Every conflict task written before 2026-08-27 looks like this. The flag can
    # sit on either leg of the pair, and nothing in the row says which — so both
    # the takedown and the keep-it check have to consider both legs.

    def _legacy_pair(self):
        """A pre-affected_leg_id conflict task on `prior`, flag on `next_pickup`.
        The shape _handle_same_day_mismatch produced when THIS leg's shift was what
        delayed the OTHER one."""
        task = create_task(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            title="Driver Conflict — legacy",
            due_at=self.fixed_now,
            priority=OperationalTask.Priority.CRITICAL,
            description="legacy shape",
            leg=self.prior,
            reservation=self.prior.reservation,
            metadata={
                "driver_id": self.driver.id,
                "conflicting_leg_id": self.next_pickup.id,
                # deliberately NO affected_leg_id
            },
        )
        flag = LegKeoi.objects.create(
            leg=self.next_pickup, category=LegKeoi.Category.DRIVER_CONFLICT,
            description="driver 20 min late", created_by=None, updated_by=None,
            operational_status=LegKeoi.OperationalStatus.NEEDS_ATTENTION,
        )
        return task, flag

    def test_legacy_shape_reassign_drops_the_partner_flag(self):
        """Reassigning the task's leg closes the task; the flag lives on the OTHER
        leg and must come down with it."""
        self._legacy_pair()
        spare = User.objects.create_user(username="tt_legacy1", first_name="Pia")
        self.prior.driver = Driver.objects.create(profile=spare, driver_type="inhouse")
        self.prior.save(update_fields=["driver"])

        self.assertFalse(
            self._open_keoi().exists(),
            "partner-leg flag survived — the legacy shape was missed",
        )

    def test_legacy_shape_keeps_the_partner_flag_while_its_task_is_open(self):
        """The dangerous direction, tested against the invariant itself rather than
        the whole autoclose pass (which would resolve the task on its own merits and
        make the takedown correct). Task OPEN, flag on the partner leg: the flag must
        stay. Closing it here is worse than leaving it up — create_task's two-hour
        cooldown blocks any re-raise, so the board goes quiet on a live conflict."""
        self._legacy_pair()

        closed = reconcile_conflict_keois()

        self.assertEqual(closed, 0)
        self.assertTrue(
            self._open_keoi().exists(),
            "reconciliation took down a flag whose conflict task is still open",
        )

    def test_save_using_the_fk_attname_still_drops_the_flag(self):
        """Django takes 'driver' and 'driver_id' equally in update_fields and builds
        update_fields from the attname itself for a deferred instance. Matching only
        'driver' silently disabled the whole takedown on those saves."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        self.assertTrue(self._open_keoi().exists())

        spare = User.objects.create_user(username="tt_attname", first_name="Sol")
        self.next_pickup.driver = Driver.objects.create(profile=spare, driver_type="inhouse")
        self.next_pickup.save(update_fields=["driver_id"])

        self.assertFalse(
            self._open_keoi().exists(),
            "a driver_id-named save skipped the takedown entirely",
        )

    def test_a_second_live_conflict_keeps_the_flag_up(self):
        """One flag, two conflicts. Closing the first task must not drop the flag
        while the second is still open — a regression to "close the flag whenever I
        close a task" would pass every other test in this class."""
        driver_free = datetime(2026, 6, 1, 16, 50)
        self._run_scan(driver_free)
        self.assertTrue(self._open_keoi().exists())

        third = self._leg("Hyatt Regency Grand Cypress", "MCO Airport", time(16, 0))
        create_task(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            title="Driver Conflict — second",
            due_at=self.fixed_now,
            priority=OperationalTask.Priority.CRITICAL,
            description="a second live conflict pointing at the same leg",
            leg=third,
            reservation=third.reservation,
            metadata={"driver_id": self.driver.id,
                      "affected_leg_id": self.next_pickup.id},
        )

        spare = User.objects.create_user(username="tt_second", first_name="Bo")
        self.prior.driver = Driver.objects.create(profile=spare, driver_type="inhouse")
        self.prior.save(update_fields=["driver"])

        self.assertTrue(
            self._open_keoi().exists(),
            "flag dropped while a second conflict was still open on that leg",
        )

    def test_a_conflict_resolved_flag_never_reactivates(self):
        """reactivate_keoi resurrects only leg_completed/leg_cancelled closes. If
        CONFLICT_RESOLVED ever joined that list, every resolved flag would come
        back the moment a leg bounced out of a terminal status."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        self.next_pickup.driver = None
        self.next_pickup.save(update_fields=["driver"])
        self.assertFalse(self._open_keoi().exists())

        self.next_pickup.status = "completed"
        self.next_pickup.save(update_fields=["status"])
        self.next_pickup.status = "in-progress"
        self.next_pickup.save(update_fields=["status"])

        self.assertFalse(
            self._open_keoi().exists(),
            "a resolved conflict flag came back from the dead",
        )

    def test_a_non_driver_save_leaves_the_flag_alone(self):
        """The gate: saves that touch neither driver nor status do no flag work."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        self.assertTrue(self._open_keoi().exists())

        self.next_pickup.pickup_time = time(16, 45)
        self.next_pickup.save(update_fields=["pickup_time"])

        self.assertTrue(self._open_keoi().exists())

    def test_reconcile_never_reaches_the_network(self):
        """This now runs inside the request cycle on every reassignment. A future
        "re-check the conflict before dropping the flag" that called the paid
        Distance Matrix here would be a live billing incident."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        spare = User.objects.create_user(username="tt_nonet", first_name="Wren")
        with patch("drivers.utils.get_drive_time") as paid:
            self.next_pickup.driver = Driver.objects.create(
                profile=spare, driver_type="inhouse")
            self.next_pickup.save(update_fields=["driver"])
        paid.assert_not_called()

    def test_a_failing_reconcile_does_not_break_the_leg_save(self):
        """_ops_leg_task_handler has no top-level try/except: an unguarded raise
        here would 500 every dispatcher reassignment."""
        self._run_scan(datetime(2026, 6, 1, 16, 50))
        spare = User.objects.create_user(username="tt_boom", first_name="Kit")
        new_driver = Driver.objects.create(profile=spare, driver_type="inhouse")

        with patch("ops.tasks.reconcile_conflict_keois", side_effect=RuntimeError("boom")):
            self.next_pickup.driver = new_driver
            self.next_pickup.save(update_fields=["driver"])   # must not raise

        self.next_pickup.refresh_from_db()
        self.assertEqual(self.next_pickup.driver_id, new_driver.id)

    def test_dispatcher_raised_flag_is_never_swept(self):
        """A person's own watch flag is theirs to close, even on a leg the system
        would have flagged."""
        boss = User.objects.create_user(username="tt_dispatcher")
        LegKeoi.objects.create(
            leg=self.next_pickup, category=LegKeoi.Category.DRIVER_CONFLICT,
            description="I want eyes on this one", created_by=boss,
            operational_status=LegKeoi.OperationalStatus.NEEDS_ATTENTION,
        )
        self._run_autoclose()
        self.assertTrue(self._open_keoi().exists())

    def test_live_conflict_keeps_its_flag(self):
        """The sweep must not disarm a flag whose conflict is still real."""
        driver_free = datetime(2026, 6, 1, 16, 50)  # 20 min late → red
        self._run_scan(driver_free)
        self.assertTrue(self._open_keoi().exists())
        self._run_autoclose(driver_free)
        self.assertTrue(self._open_keoi().exists())
