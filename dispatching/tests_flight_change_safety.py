"""Tests for the flight-change safety workstream.

Run with:  ./manage.py test dispatching.tests_flight_change_safety

Covers:
  * classify_refresh_row's Minor Change bucket: 5-29 min snapshot moves and
    pickup-vs-flight drifts get an info row; <5 min stays OK; >=30 min keeps
    the existing Needs Review escalation.
  * auto_create_flight_verify_tasks skips minor rows (FYI only, no task).
  * build_review_summary counts the new bucket + exposes the thresholds.
  * match_leg_time_to_flight: unconditional conflict detection in the JSON
    summary, the durable AuditLog row, and the "time changed" stamp fields.
  * acknowledge_time_change clears the board badge state (and rejects a
    string leg_ids payload instead of char-iterating it).
  * The Leg.save() pickup-change hook: stamps on the update_fields fast path,
    stays quiet on unchanged saves, preserves the earliest "was" across
    successive moves, clears the badge on a net-zero A→B→A revert, and
    doesn't disturb the driver-change status reset.
  * apply_pickup_time_move (shared stamp+audit core) — guest-triggered moves
    stamp the badge + AuditLog too, and net-zero reverts clear the badge.
  * flight_verification_public auto-adjust goes through the shared helper.
  * Bulk-refresh new_conflicts includes tasks anchored on the driver's next
    leg (not just the refreshed arrivals).
"""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from ops.models import OperationalTask, StaffActivity
from rates.models import Vehicle, Location, Route, Rate
from reservations.models import AuditLog, Customer, Flight, Leg, Reservation

from dispatching.flight_refresh_review import (
    STATUS_MINOR,
    STATUS_NEEDS_REVIEW,
    STATUS_OK,
    auto_create_flight_verify_tasks,
    build_review_summary,
    classify_refresh_row,
    snapshot_flight_state,
)
from dispatching.pickup_moves import apply_pickup_time_move

User = get_user_model()


def _aware(day, t):
    """Aware datetime on `day` at time `t` in the project timezone."""
    return timezone.make_aware(datetime.combine(day, t))


class _FlightChangeFixtureMixin:
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
            phone_number="5551234567",
        )
        cls.target_date = timezone.localdate() + timedelta(days=1)

    def _res(self):
        return Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )

    def _arrival_leg(self, pickup_time, arrival_time, **kw):
        """MCO → Disney arrival leg whose flight's best arrival is arrival_time."""
        flight = Flight.objects.create(
            airline="DL", flight_number="123", flight_iata="DL123",
            status="Scheduled",
            estimated_gate_arrival_local=_aware(self.target_date, arrival_time),
        )
        defaults = dict(
            reservation=self._res(),
            pickup_date=self.target_date,
            pickup_time=pickup_time,
            pickup_location="MCO",
            dropoff_location="Disney",
            route=self.route,
            status="confirmed",
            flight_information=flight,
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class ClassifyMinorBucketTests(_FlightChangeFixtureMixin, TestCase):
    """Minor Change bucket: the 5-29 min moves dispatchers used to miss."""

    def _classify(self, leg, snapshot):
        return classify_refresh_row(
            leg, snapshot, {"leg_id": leg.id, "success": True},
            threshold_minutes=30, minor_threshold_minutes=5,
        )

    def test_ten_min_snapshot_delta_is_minor(self):
        leg = self._arrival_leg(time(10, 10), time(10, 0))
        snapshot = snapshot_flight_state(leg.flight_information)
        # Refresh moved the arrival 10 min later; pickup already matches it.
        leg.flight_information.estimated_gate_arrival_local = _aware(
            self.target_date, time(10, 10)
        )
        row = self._classify(leg, snapshot)
        self.assertEqual(row["status"], STATUS_MINOR)
        self.assertEqual(row["issue_code"], "arrival_changed_minor")
        self.assertEqual(row["severity"], "info")
        self.assertIn("10 min", row["issue_label"])

    def test_three_min_snapshot_delta_stays_ok(self):
        leg = self._arrival_leg(time(10, 3), time(10, 0))
        snapshot = snapshot_flight_state(leg.flight_information)
        leg.flight_information.estimated_gate_arrival_local = _aware(
            self.target_date, time(10, 3)
        )
        row = self._classify(leg, snapshot)
        self.assertEqual(row["status"], STATUS_OK)

    def test_45_min_snapshot_delta_still_needs_review(self):
        leg = self._arrival_leg(time(10, 45), time(10, 0))
        snapshot = snapshot_flight_state(leg.flight_information)
        leg.flight_information.estimated_gate_arrival_local = _aware(
            self.target_date, time(10, 45)
        )
        row = self._classify(leg, snapshot)
        self.assertEqual(row["status"], STATUS_NEEDS_REVIEW)
        self.assertEqual(row["issue_code"], "arrival_changed")

    def test_15_min_pickup_mismatch_is_minor(self):
        # Arrival unchanged since last check, but pickup sits 15 min off it.
        leg = self._arrival_leg(time(9, 45), time(10, 0))
        snapshot = snapshot_flight_state(leg.flight_information)
        row = self._classify(leg, snapshot)
        self.assertEqual(row["status"], STATUS_MINOR)
        self.assertEqual(row["issue_code"], "pickup_flight_minor_mismatch")
        # mismatch payload uses the minor threshold, so the minutes surface too
        self.assertEqual(row["mismatch_minutes"], 15)

    def test_auto_create_tasks_skips_minor_rows(self):
        leg = self._arrival_leg(time(9, 45), time(10, 0))
        snapshot = snapshot_flight_state(leg.flight_information)
        row = self._classify(leg, snapshot)
        self.assertEqual(row["status"], STATUS_MINOR)
        created = auto_create_flight_verify_tasks([row])
        self.assertEqual(created, 0)
        self.assertFalse(
            OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION
            ).exists()
        )

    def test_build_review_summary_counts_and_thresholds(self):
        legs = [
            self._arrival_leg(time(10, 10), time(10, 0)),  # minor (arrival moved)
            self._arrival_leg(time(11, 0), time(11, 0)),   # ok
            self._arrival_leg(time(12, 45), time(12, 0)),  # needs review (mismatch)
        ]
        rows = []
        for leg in legs:
            snapshot = snapshot_flight_state(leg.flight_information)
            rows.append(self._classify(leg, snapshot))
        # move the first leg's arrival 10 min so it lands in the minor bucket
        legs[0].flight_information.estimated_gate_arrival_local = _aware(
            self.target_date, time(10, 10)
        )
        snapshot = snapshot_flight_state(
            Flight.objects.get(id=legs[0].flight_information_id)
        )
        rows[0] = self._classify(legs[0], snapshot)

        summary = build_review_summary(rows, minor_threshold_minutes=5, threshold_minutes=30)
        self.assertEqual(summary["totals"]["total"], 3)
        self.assertEqual(summary["totals"][STATUS_MINOR], 1)
        self.assertEqual(summary["totals"][STATUS_OK], 1)
        self.assertEqual(summary["totals"][STATUS_NEEDS_REVIEW], 1)
        self.assertEqual(
            summary["thresholds"], {"minor": 5, "review": 30, "manual": 120}
        )


@patch("drivers.utils.get_drive_time", return_value=None)
class MatchEndpointTests(_FlightChangeFixtureMixin, TestCase):
    """Match → unconditional conflict summary + audit trail + ack cycle."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="dispatcher", password="x", is_staff=True
        )
        self.client.force_login(self.staff)
        driver_user = User.objects.create_user(username="alex", password="x")
        self.driver = Driver.objects.create(
            profile=driver_user, driver_type="inhouse", is_active=True
        )
        # Prior job: 8:00 AM departure run keeps the driver busy well past the
        # flight's 8:15 landing (fallback trip duration is 75 min).
        self.departure = Leg.objects.create(
            reservation=self._res(),
            pickup_date=self.target_date,
            pickup_time=time(8, 0),
            pickup_location="Disney",
            dropoff_location="MCO",
            route=self.route,
            status="confirmed",
            driver=self.driver,
        )
        # Arrival booked 8:40, flight now landing 8:15.
        self.arrival = self._arrival_leg(
            time(8, 40), time(8, 15), driver=self.driver
        )

    def _match(self, leg):
        return self.client.post(
            reverse("match_leg_time_to_flight"),
            json.dumps({"leg_id": leg.id}),
            content_type="application/json",
        )

    def test_match_reports_conflicts_and_stamps_change(self, _mock_drive):
        resp = self._match(self.arrival)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])

        summary = data["summary"]
        self.assertEqual(summary["old_time"], "8:40 AM")
        self.assertEqual(summary["new_time"], "8:15 AM")
        self.assertEqual(summary["delta_minutes"], -25)
        self.assertTrue(summary["conflicts"], "expected the 8:00 job to conflict")
        conflict = summary["conflicts"][0]
        self.assertEqual(conflict["reservation_id"], self.departure.reservation_id)
        self.assertGreater(conflict["conflict_minutes"], 0)
        self.assertIn(conflict["tier"], ("red", "amber"))

        # Durable audit trail
        audit = AuditLog.objects.filter(
            model_name="Leg", object_id=self.arrival.id, field_name="pickup_time"
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.old_value, "8:40 AM")
        self.assertEqual(audit.new_value, "8:15 AM")
        self.assertEqual(audit.user, self.staff)
        self.assertEqual(audit.notes, "Flight match")
        self.assertTrue(
            StaffActivity.objects.filter(
                user=self.staff,
                action_type=StaffActivity.ActionType.FLIGHT_MATCHED,
                metadata__leg_id=self.arrival.id,
            ).exists()
        )

        # Board badge stamp fields
        self.arrival.refresh_from_db()
        self.assertEqual(self.arrival.pickup_time, time(8, 15))
        self.assertIsNotNone(self.arrival.pickup_time_changed_at)
        self.assertEqual(self.arrival.pickup_time_was, time(8, 40))
        self.assertIsNone(self.arrival.pickup_change_ack_at)
        self.assertTrue(self.arrival.has_unacked_time_change)

    def test_acknowledge_clears_badge_state(self, _mock_drive):
        self._match(self.arrival)
        resp = self.client.post(
            reverse("acknowledge_time_change"),
            json.dumps({"leg_id": self.arrival.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "acked": 1})
        self.arrival.refresh_from_db()
        self.assertIsNotNone(self.arrival.pickup_change_ack_at)
        self.assertFalse(self.arrival.has_unacked_time_change)

    def test_acknowledge_requires_leg_id(self, _mock_drive):
        resp = self.client.post(
            reverse("acknowledge_time_change"),
            json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_acknowledge_rejects_string_leg_ids(self, _mock_drive):
        # {"leg_ids": "57"} would char-iterate into legs 5 and 7 — must 400.
        self._match(self.arrival)
        resp = self.client.post(
            reverse("acknowledge_time_change"),
            json.dumps({"leg_ids": str(self.arrival.id)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.arrival.refresh_from_db()
        self.assertTrue(self.arrival.has_unacked_time_change)


class SaveHookTests(_FlightChangeFixtureMixin, TestCase):
    """Leg.save() stamps pickup-time changes — including the fast path."""

    def test_fast_path_update_fields_stamps(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        leg.pickup_time = time(10, 30)
        leg.save(update_fields=["pickup_time"])
        leg.refresh_from_db()
        self.assertIsNotNone(leg.pickup_time_changed_at)
        self.assertEqual(leg.pickup_time_was, time(10, 0))
        self.assertIsNone(leg.pickup_change_ack_at)
        self.assertTrue(leg.has_unacked_time_change)

    def test_unchanged_save_does_not_stamp(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        leg.save()
        leg.refresh_from_db()
        self.assertIsNone(leg.pickup_time_changed_at)
        self.assertIsNone(leg.pickup_time_was)
        self.assertFalse(leg.has_unacked_time_change)

    def test_new_leg_creation_does_not_stamp(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        self.assertIsNone(leg.pickup_time_changed_at)

    def test_successive_moves_preserve_earliest_was(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        leg.pickup_time = time(10, 30)
        leg.save(update_fields=["pickup_time"])
        first_changed_at = Leg.objects.get(id=leg.id).pickup_time_changed_at
        # Second move before anyone acknowledged: "was" must keep 10:00.
        leg.pickup_time = time(11, 0)
        leg.save(update_fields=["pickup_time"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time_was, time(10, 0))
        self.assertGreaterEqual(leg.pickup_time_changed_at, first_changed_at)

    def test_move_after_ack_captures_new_was(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        leg.pickup_time = time(10, 30)
        leg.save(update_fields=["pickup_time"])
        Leg.objects.filter(id=leg.id).update(pickup_change_ack_at=timezone.now())
        leg.refresh_from_db()
        leg.pickup_time = time(11, 0)
        leg.save(update_fields=["pickup_time"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time_was, time(10, 30))
        self.assertTrue(leg.has_unacked_time_change)

    def test_net_zero_revert_clears_pending_change(self):
        # A→B→A before anyone acked: no change is left to acknowledge, so
        # the pending badge state must be cleared, not left as "was 10:00".
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        leg.pickup_time = time(10, 30)
        leg.save(update_fields=["pickup_time"])
        leg.pickup_time = time(10, 0)
        leg.save(update_fields=["pickup_time"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(10, 0))
        self.assertIsNone(leg.pickup_time_changed_at)
        self.assertIsNone(leg.pickup_time_was)
        self.assertIsNone(leg.pickup_change_ack_at)
        self.assertFalse(leg.has_unacked_time_change)

    def test_acked_change_then_revert_still_stamps(self):
        # Once the first move was ACKNOWLEDGED, moving back is a real new
        # change (the dispatcher saw 10:30 stand) — it must stamp normally.
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        leg.pickup_time = time(10, 30)
        leg.save(update_fields=["pickup_time"])
        Leg.objects.filter(id=leg.id).update(pickup_change_ack_at=timezone.now())
        leg.refresh_from_db()
        leg.pickup_time = time(10, 0)
        leg.save(update_fields=["pickup_time"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time_was, time(10, 30))
        self.assertTrue(leg.has_unacked_time_change)

    def test_driver_change_save_unaffected(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        driver_user = User.objects.create_user(username="sam", password="x")
        driver = Driver.objects.create(
            profile=driver_user, driver_type="inhouse", is_active=True
        )
        leg.driver = driver
        leg.save(update_fields=["driver"])
        leg.refresh_from_db()
        # Driver-change status reset still runs; pickup stamp fields untouched.
        self.assertEqual(leg.status, "in-progress")
        self.assertIsNone(leg.pickup_time_changed_at)
        self.assertFalse(leg.has_unacked_time_change)


class PickupMoveHelperTests(_FlightChangeFixtureMixin, TestCase):
    """apply_pickup_time_move — the shared stamp+audit core used by both the
    dispatcher match endpoints and the guest flight-verify auto-adjust."""

    def test_guest_move_stamps_and_audits_without_user(self):
        leg = self._arrival_leg(time(8, 40), time(8, 15))
        moved = apply_pickup_time_move(
            leg, time(8, 15), user=None,
            note="Guest flight verification auto-adjust",
        )
        self.assertTrue(moved)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(8, 15))
        self.assertEqual(leg.pickup_time_was, time(8, 40))
        self.assertIsNotNone(leg.pickup_time_changed_at)
        self.assertIsNone(leg.pickup_change_ack_at)
        self.assertTrue(leg.has_unacked_time_change)

        audit = AuditLog.objects.get(
            model_name="Leg", object_id=leg.id, field_name="pickup_time"
        )
        self.assertIsNone(audit.user)
        self.assertEqual(audit.username, "guest")
        self.assertEqual(audit.old_value, "8:40 AM")
        self.assertEqual(audit.new_value, "8:15 AM")
        self.assertEqual(audit.notes, "Guest flight verification auto-adjust")

    def test_unchanged_time_is_noop(self):
        leg = self._arrival_leg(time(8, 40), time(8, 40))
        self.assertFalse(apply_pickup_time_move(leg, time(8, 40)))
        self.assertFalse(
            AuditLog.objects.filter(
                model_name="Leg", object_id=leg.id, field_name="pickup_time"
            ).exists()
        )

    def test_net_zero_revert_clears_pending_change(self):
        leg = self._arrival_leg(time(8, 40), time(8, 15))
        apply_pickup_time_move(leg, time(8, 15))
        self.assertTrue(apply_pickup_time_move(leg, time(8, 40)))
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(8, 40))
        self.assertIsNone(leg.pickup_time_changed_at)
        self.assertIsNone(leg.pickup_time_was)
        self.assertIsNone(leg.pickup_change_ack_at)
        self.assertFalse(leg.has_unacked_time_change)
        # Both moves still leave their durable audit rows.
        self.assertEqual(
            AuditLog.objects.filter(
                model_name="Leg", object_id=leg.id, field_name="pickup_time"
            ).count(),
            2,
        )

    def test_successive_moves_preserve_earliest_was(self):
        leg = self._arrival_leg(time(8, 40), time(8, 15))
        apply_pickup_time_move(leg, time(8, 15))
        apply_pickup_time_move(leg, time(9, 0))
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time_was, time(8, 40))
        self.assertTrue(leg.has_unacked_time_change)


class GuestFlightVerifyAutoAdjustTests(_FlightChangeFixtureMixin, TestCase):
    """flight_verification_public POST: the pickup auto-adjust must run
    through the shared stamped write path (purple badge + AuditLog), not a
    bare .update(pickup_time=...)."""

    def _post_verify(self, leg, new_arrival_time):
        from dispatching.flight_verify_email import make_verify_token
        token = make_verify_token(leg.id)

        def _fake_refresh(flight, refresh_leg, aeroapi):
            # Simulate AeroAPI repopulating the arrival for the new flight.
            Flight.objects.filter(pk=flight.pk).update(
                scheduled_gate_arrival_local=_aware(
                    self.target_date, new_arrival_time
                ),
                last_updated=timezone.now(),
            )
            return {"success": True}

        with patch(
            "dispatching.views._refresh_one_flight", side_effect=_fake_refresh
        ), patch(
            "dispatching.flight_verify_email.send_flight_updated_notifications"
        ):
            return self.client.post(
                reverse("flight_verification_public", args=[token]),
                {"airline": "DL", "flight_number": "123"},
            )

    def test_auto_adjust_stamps_badge_and_audit(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        resp = self._post_verify(leg, time(10, 30))
        self.assertEqual(resp.status_code, 200)

        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(10, 30))
        self.assertIsNotNone(leg.pickup_time_changed_at)
        self.assertEqual(leg.pickup_time_was, time(10, 0))
        self.assertIsNone(leg.pickup_change_ack_at)
        self.assertTrue(leg.has_unacked_time_change)

        audit = AuditLog.objects.filter(
            model_name="Leg", object_id=leg.id, field_name="pickup_time"
        ).first()
        self.assertIsNotNone(audit)
        self.assertIsNone(audit.user)
        self.assertEqual(audit.username, "guest")
        self.assertEqual(audit.notes, "Guest flight verification auto-adjust")
        self.assertEqual(audit.old_value, "10:00 AM")
        self.assertEqual(audit.new_value, "10:30 AM")

    def test_sub_15_min_shift_not_adjusted(self):
        leg = self._arrival_leg(time(10, 0), time(10, 0))
        resp = self._post_verify(leg, time(10, 10))
        self.assertEqual(resp.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(10, 0))
        self.assertIsNone(leg.pickup_time_changed_at)
        self.assertFalse(leg.has_unacked_time_change)


class BulkRefreshConflictSummaryTests(_FlightChangeFixtureMixin, TestCase):
    """new_conflicts must surface tasks anchored on the driver's NEXT leg
    (often a departure that wasn't refreshed), not just refreshed arrivals."""

    def test_new_conflicts_includes_next_leg_anchored_task(self):
        from ops.models import OperationalTask
        from dispatching.views import (
            _flight_refresh_get,
            _run_bulk_flight_refresh,
        )

        driver_user = User.objects.create_user(username="bulk_drv", password="x")
        driver = Driver.objects.create(
            profile=driver_user, driver_type="inhouse", is_active=True
        )
        arrival = self._arrival_leg(time(8, 40), time(8, 15), driver=driver)
        departure = Leg.objects.create(
            reservation=self._res(),
            pickup_date=self.target_date,
            pickup_time=time(9, 30),
            pickup_location="Disney",
            dropoff_location="MCO",
            route=self.route,
            status="confirmed",
            driver=driver,
        )

        def _fake_scan():
            # _scan_driver_overlaps anchors the task on the leg the driver is
            # late TO — the departure, which was NOT in the refreshed set.
            OperationalTask.objects.create(
                task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
                title="Driver conflict",
                due_at=timezone.now(),
                leg=departure,
                reservation=departure.reservation,
                metadata={"driver_name": str(driver), "conflict_minutes": 25},
            )

        with patch(
            "dispatching.views._refresh_single_flight",
            side_effect=lambda leg: {"leg_id": leg.id, "success": True},
        ), patch("ops.tasks._scan_driver_overlaps", side_effect=_fake_scan):
            _run_bulk_flight_refresh("conflict-test-task", [arrival.id])

        data = _flight_refresh_get("conflict-test-task")
        self.assertIsNotNone(data)
        self.assertEqual(data["status"], "completed")
        conflicts = data["summary"]["new_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["tier"], "red")
        self.assertEqual(conflicts[0]["driver_name"], str(driver))
        self.assertEqual(conflicts[0]["conflict_minutes"], 25)
        self.assertEqual(
            conflicts[0]["reservation_id"], departure.reservation_id
        )


class BulkRefreshCrossWorkerStateTests(_FlightChangeFixtureMixin, TestCase):
    """
    Refresh progress must survive being read by a DIFFERENT gunicorn worker
    than the one that started it.

    Regression: the state lived in the Django cache, which is per-process
    LocMemCache without REDIS_URL. With `gunicorn --workers 3` the status poll
    round-robined onto a worker that had never seen the task and 404'd
    "Refresh task not found" on the first poll, so the review modal never
    opened even though the flights had actually been refreshed.
    """

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            username="xw_staff", password="x", is_staff=True
        )
        self.client.force_login(self.staff)

    def test_status_survives_a_cold_cache(self):
        """A worker with a totally empty cache must still see the task."""
        from django.core.cache import cache
        from dispatching.views import _flight_refresh_set

        _flight_refresh_set("xw-task", {"status": "running", "processed": 2, "total": 5})

        # Simulate the poll landing on a worker whose LocMemCache knows nothing.
        cache.clear()

        resp = self.client.get(
            reverse("refresh_all_flights_status", args=["xw-task"])
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["processed"], 2)

    def test_completed_run_delivers_summary_to_a_cold_cache(self):
        """The review-modal payload (summary) must reach the polling worker."""
        from django.core.cache import cache
        from dispatching.views import _run_bulk_flight_refresh

        arrival = self._arrival_leg(time(8, 40), time(8, 15))

        with patch(
            "dispatching.views._refresh_single_flight",
            side_effect=lambda leg: {"leg_id": leg.id, "success": True},
        ), patch("ops.tasks._scan_driver_overlaps"):
            _run_bulk_flight_refresh("xw-done", [arrival.id])

        cache.clear()

        resp = self.client.get(
            reverse("refresh_all_flights_status", args=["xw-done"])
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "completed")
        # This is what the JS gates openRefreshReviewModal() on.
        self.assertIsNotNone(body["summary"])
        self.assertIn("rows", body["summary"])
        self.assertIn("totals", body["summary"])

    def test_unknown_task_still_404s(self):
        resp = self.client.get(
            reverse("refresh_all_flights_status", args=["no-such-task"])
        )
        self.assertEqual(resp.status_code, 404)

    def test_prune_drops_only_old_rows(self):
        from dispatching.models import FlightRefreshTask
        from dispatching.views import _flight_refresh_set, _flight_refresh_prune

        _flight_refresh_set("fresh", {"status": "completed"})
        _flight_refresh_set("stale", {"status": "completed"})
        FlightRefreshTask.objects.filter(task_id="stale").update(
            created_at=timezone.now() - timedelta(hours=48)
        )

        _flight_refresh_prune()

        self.assertTrue(FlightRefreshTask.objects.filter(task_id="fresh").exists())
        self.assertFalse(FlightRefreshTask.objects.filter(task_id="stale").exists())
