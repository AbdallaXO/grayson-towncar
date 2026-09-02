"""A flight re-timed on a FUTURE board must re-check the driver's chain.

Founder report 2026-08-09. A 2:03 PM MCO arrival and a 3:30 PM Animal Kingdom departure sat
on one driver for the NEXT day's board. The arrival's flight had already slid later — the
board redrew its clear time from the new flight and showed ~3:40 — but the feasibility
verdict stayed frozen at whatever it was when the leg was assigned, so nothing ever said the
3:30 no longer fit. There was a full day of warning.

Cause: `_scan_flight_mismatches` branched on `is_same_day`. Same-day ran
`detect_driver_conflicts`; a future-dated shift got a guest-verification task only — "does
the customer know their flight moved?" — and nobody asked "does this still fit the driver?"

Pinned here:
  * a future-dated flight shift that breaks the chain raises a DRIVER_CONFLICT task;
  * it FLAGS and never ACTS (no reassignment — the founder's standing rule);
  * priority steps up inside 48 hours;
  * the scan makes NO paid Distance Matrix calls (it used to make one per leg pair, which
    is why widening it past today was unaffordable before);
  * a SUB-30-MINUTE drift still triggers the chain re-check. This was the deeper half of the
    bug: both the scan gate and the guest-notification gate used one number (MINOR_THRESHOLD,
    30 min), and the founder's flight moved roughly 13 — so the leg was skipped outright, on
    every date, before the same-day/future branch was ever reached.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from drivers.models import Driver
from ops.models import OperationalTask
from ops.tasks import _scan_flight_mismatches
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Flight, Leg, Reservation


class _FutureBoardFixture(TestCase):
    """One driver, one future-dated board, an arrival whose flight has moved."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="van", capacity=11, luggage_capacity=10)
        cls.route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Deborah", last_name="Peters", email="d@example.com",
            phone_number="5550001111")
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user(username="ff_driver", first_name="Alex"),
            driver_type="inhouse")
        cls.affiliate = Driver.objects.create(
            profile=User.objects.create_user(username="ff_aff", first_name="Farmed"),
            driver_type="affiliate")

    def _res(self):
        return Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))

    def _leg(self, pickup, dropoff, pickup_time, day, **kw):
        defaults = dict(
            reservation=self._res(), pickup_date=day, pickup_time=pickup_time,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route,
            status="confirmed", driver=self.driver)
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _build_board(self, days_out=1, flight_arrival_time=time(14, 16), driver=None):
        """The founder's pair, `days_out` days from today.

        Arrival booked 2:03 PM whose flight now says `flight_arrival_time` (default 2:16 —
        past the 30-min MINOR_THRESHOLD once the pax-ready grace is applied), then a 3:30 PM
        departure across Disney property on the same driver.
        """
        day = timezone.localdate() + timedelta(days=days_out)
        arrival = self._leg(
            "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
            "Disney's Port Orleans Resort - French Quarter, Orleans Drive, "
            "Lake Buena Vista, FL, USA",
            time(14, 3), day)
        if driver is not None:
            arrival.driver = driver
        arrival.flight_information = Flight.objects.create(
            airline="UA", flight_number="2396",
            scheduled_arrival_local=datetime.combine(day, flight_arrival_time))
        arrival.save()

        departure = self._leg(
            "Disney's Animal Kingdom Lodge, Osceola Parkway, Lake Buena Vista, FL, USA",
            "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
            time(15, 30), day)
        if driver is not None:
            departure.driver = driver
            departure.save()
        return arrival, departure, day

    def _conflict_tasks(self):
        return OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT)

    def _tight_turn_tasks(self):
        return OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.TIGHT_TURN)


class FutureConflictIsRaisedTests(_FutureBoardFixture):
    def test_future_flight_shift_raises_a_driver_conflict(self):
        """The whole point: this used to produce a guest-verification task and nothing else.
        A big enough drift (37 min, past TURN_TIGHT_SLACK_MIN) genuinely won't make it."""
        self._build_board(days_out=1, flight_arrival_time=time(14, 40))
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 1)

    def test_the_task_explains_it_in_dispatcher_language(self):
        arrival, departure, day = self._build_board(days_out=1, flight_arrival_time=time(14, 40))
        _scan_flight_mismatches()
        task = self._conflict_tasks().first()
        self.assertIn("Alex", task.title)
        self.assertIn("3:30 PM", task.description)
        self.assertIn("Nothing has been changed automatically", task.description)
        self.assertTrue(task.metadata.get("future_board"))
        self.assertEqual(task.metadata["conflicting_leg_id"], departure.id)
        self.assertEqual(task.metadata["pickup_date"], str(day))

    def test_it_flags_and_never_acts(self):
        """Founder's standing rule: nothing automated touches drivers."""
        arrival, departure, _ = self._build_board(days_out=1)
        _scan_flight_mismatches()
        arrival.refresh_from_db()
        departure.refresh_from_db()
        self.assertEqual(arrival.driver_id, self.driver.id)
        self.assertEqual(departure.driver_id, self.driver.id)
        self.assertEqual(departure.pickup_time, time(15, 30))
        self.assertEqual(arrival.pickup_time, time(14, 3))

    def test_inside_48h_is_critical_beyond_is_high(self):
        self._build_board(days_out=1, flight_arrival_time=time(14, 40))
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().first().priority,
                         OperationalTask.Priority.CRITICAL)

        OperationalTask.objects.all().delete()
        Leg.objects.all().delete()
        self._build_board(days_out=5, flight_arrival_time=time(14, 40))
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().first().priority,
                         OperationalTask.Priority.HIGH)

    def test_repeat_scans_do_not_pile_up_tasks(self):
        """The scanner runs every 30 minutes; create_task dedups on leg + type."""
        self._build_board(days_out=1, flight_arrival_time=time(14, 40))
        _scan_flight_mismatches()
        _scan_flight_mismatches()
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 1)

    def test_guest_verification_task_is_still_raised_alongside(self):
        """Two questions, two people — the customer still needs telling, when the drift is
        big enough to be worth a phone call (>= MINOR_THRESHOLD)."""
        self._build_board(days_out=1, flight_arrival_time=time(15, 10))  # 67 min late
        _scan_flight_mismatches()
        self.assertEqual(
            OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION).count(), 1)
        self.assertEqual(self._conflict_tasks().count(), 1)


class SubThresholdDriftTests(_FutureBoardFixture):
    """The half of the bug that kept the founder's leg from being looked at AT ALL."""

    def test_thirteen_minute_drift_still_rechecks_the_chain(self):
        """2:03 booked, flight now 2:16 — 13 min, well under the 30-min guest bar, and
        enough to break a chain the engine built at zero slack. 13 min is past
        TIGHT_TURN_RED_AFTER_MIN (10), so it's a genuine CRITICAL conflict, not just
        a tight_turn watch — the point pinned here is that the chain gets RE-EXAMINED
        at all (the founder's bug was the scan skipping it outright)."""
        self._build_board(days_out=1, flight_arrival_time=time(14, 16))
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 1)
        self.assertEqual(self._tight_turn_tasks().count(), 0)

    def test_a_smaller_drift_inside_the_grace_is_amber(self):
        """A drift small enough to stay inside TIGHT_TURN_RED_AFTER_MIN (10) is still
        re-examined, but correctly triaged as "keep an eye on it" — not a false
        CRITICAL emergency. This is the exact alert-fatigue bug the driver-conflict
        severity fix targets, on the future-board code path."""
        self._build_board(days_out=1, flight_arrival_time=time(14, 10))  # 7 min drift
        _scan_flight_mismatches()
        self.assertEqual(self._tight_turn_tasks().count(), 1)
        self.assertEqual(self._conflict_tasks().count(), 0)

    def test_it_does_not_pester_the_guest_over_thirteen_minutes(self):
        """The whole reason we did not just lower MINOR_THRESHOLD."""
        self._build_board(days_out=1, flight_arrival_time=time(14, 16))
        _scan_flight_mismatches()
        self.assertEqual(
            OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION).count(), 0)

    def test_noise_below_the_recheck_floor_is_ignored(self):
        """A 2-minute wobble is not a flight moving."""
        self._build_board(days_out=1, flight_arrival_time=time(14, 5))
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 0)


class NoFalsePositiveTests(_FutureBoardFixture):
    def test_a_chain_that_still_fits_raises_no_conflict(self):
        """Flight moved, but the next job is hours away — verification only, no conflict."""
        day = timezone.localdate() + timedelta(days=2)
        arrival = self._leg(
            "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
            "Disney's Port Orleans Resort - French Quarter, Orleans Drive, "
            "Lake Buena Vista, FL, USA",
            time(14, 3), day)
        arrival.flight_information = Flight.objects.create(
            airline="UA", flight_number="2396",
            scheduled_arrival_local=datetime.combine(day, time(14, 16)))
        arrival.save()
        self._leg(
            "Disney's Animal Kingdom Lodge, Osceola Parkway, Lake Buena Vista, FL, USA",
            "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
            time(21, 0), day)
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 0)

    def test_unassigned_leg_raises_no_conflict(self):
        self._build_board(days_out=1)
        Leg.objects.all().update(driver=None)
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 0)

    def test_affiliate_leg_raises_no_conflict(self):
        """Affiliates run their own dispatch — not our chain to police."""
        self._build_board(days_out=1)
        Leg.objects.all().update(driver=self.affiliate)
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 0)

    def test_a_lone_leg_raises_no_conflict(self):
        day = timezone.localdate() + timedelta(days=1)
        arrival = self._leg(
            "Orlando International Airport (MCO), Jeff Fuqua Boulevard, Orlando, FL, USA",
            "Disney's Port Orleans Resort - French Quarter, Orleans Drive, "
            "Lake Buena Vista, FL, USA",
            time(14, 3), day)
        arrival.flight_information = Flight.objects.create(
            airline="UA", flight_number="2396",
            scheduled_arrival_local=datetime.combine(day, time(14, 16)))
        arrival.save()
        _scan_flight_mismatches()
        self.assertEqual(self._conflict_tasks().count(), 0)


class NoPaidApiCallsTests(_FutureBoardFixture):
    """Widening this scan past today was only affordable because the paid per-pair
    Distance Matrix call was removed first. If it comes back, this fails."""

    def test_the_whole_scan_makes_no_distance_matrix_calls(self):
        self._build_board(days_out=1, flight_arrival_time=time(14, 40))
        with patch("drivers.utils.get_drive_time") as paid:
            _scan_flight_mismatches()
            paid.assert_not_called()
        self.assertEqual(self._conflict_tasks().count(), 1,
                         "and it still found the conflict without paying for it")

    def test_reposition_helper_is_free_and_agrees_with_the_scheduler(self):
        from dispatching.analytics import categorize_location
        from dispatching.scheduler import chain_repo_minutes
        from ops.tasks import _reposition_minutes

        pofq = ("Disney's Port Orleans Resort - French Quarter, Orleans Drive, "
                "Lake Buena Vista, FL, USA")
        akl = "Disney's Animal Kingdom Lodge, Osceola Parkway, Lake Buena Vista, FL, USA"
        with patch("drivers.utils.get_drive_time") as paid:
            got = _reposition_minutes(pofq, akl)
            paid.assert_not_called()
        self.assertEqual(got, chain_repo_minutes(
            pofq, akl, categorize_location(pofq), categorize_location(akl)))
