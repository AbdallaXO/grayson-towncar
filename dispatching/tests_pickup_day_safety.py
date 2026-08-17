"""Tests for the wrong-day pickup match and the leg change timeline.

Run with:  ./manage.py test dispatching.tests_pickup_day_safety

Background — the incident these guard against
---------------------------------------------
A flight landing 11:25 PM on Aug 10 was matched onto a leg dated Aug 11. The
match endpoint wrote only pickup_time, so the pickup became Aug 11 11:25 PM:
~23 hours after the guest actually landed. Nobody was dispatched, and the
guest was left at MCO.

Two separate defects made that possible, and both are covered here:

  1. _flight_match_skip_reason already refused wrong-day flights, but only the
     BULK endpoint ever called it. The single-leg button did not.
  2. The move was written with queryset.update(), which bypasses
     simple_history — so it left no history row, and surfaced hours later
     folded into the next person's save (a driver tapping Accept), under
     their name.

Covers:
  * single-leg match refuses a wrong-day flight and changes nothing
  * confirm="move_date" moves date AND time; confirm="keep_date" moves time only
  * same-day matches still work in one call (no new friction)
  * cancelled / diverted flights are refused
  * a day move stamps pickup_date_was and raises pickup_day_moved
  * a pickup move writes its own history row, attributed correctly
  * a later save by someone else does NOT absorb an earlier pickup move
  * the timeline reports move and status as separate, correctly-attributed
    events, and re-dates legacy bundled rows to when they really happened
"""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from simple_history.utils import get_history_manager_for_model

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import AuditLog, Customer, Flight, Leg, Reservation

from dispatching.leg_timeline import build_leg_timeline, timeline_summary
from dispatching.pickup_moves import apply_pickup_time_move

User = get_user_model()


def _aware(day, t):
    return timezone.make_aware(datetime.combine(day, t))


class _PickupFixture:
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
        cls.dispatcher = User.objects.create_user(
            username="iris", password="pw", is_staff=True, first_name="Iris",
            last_name="Costa",
        )
        cls.driver_user = User.objects.create_user(
            username="shelley", password="pw", first_name="Shelley",
        )
        # Pickup sits just after midnight; the flight lands the evening before.
        cls.pickup_date = timezone.localdate() + timedelta(days=2)
        cls.arrival_date = cls.pickup_date - timedelta(days=1)

    def _leg(self, pickup_time=time(0, 15), arrival_time=time(23, 25),
             arrival_date=None, status="Scheduled"):
        flight = Flight.objects.create(
            airline="AA", flight_number="1234", flight_iata="AA1234",
            status=status,
            estimated_gate_arrival_local=_aware(
                arrival_date if arrival_date is not None else self.arrival_date,
                arrival_time,
            ),
        )
        reservation = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )
        return Leg.objects.create(
            reservation=reservation,
            pickup_date=self.pickup_date,
            pickup_time=pickup_time,
            pickup_location="Orlando International Airport (MCO)",
            dropoff_location="Walt Disney World Swan",
            route=self.route,
            status="confirmed",
            flight_information=flight,
        )

    def _match(self, leg, confirm=None):
        body = {"leg_id": leg.id}
        if confirm:
            body["confirm"] = confirm
        return self.client.post(
            reverse("match_leg_time_to_flight"),
            data=json.dumps(body),
            content_type="application/json",
        )


class WrongDayMatchGuardTests(_PickupFixture, TestCase):
    def setUp(self):
        self.client.force_login(self.dispatcher)

    def test_wrong_day_match_is_refused_and_changes_nothing(self):
        """The exact incident: 11:25 PM arrival, pickup dated the next day."""
        leg = self._leg()
        response = self._match(leg)

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["needs_confirmation"], "wrong_day")

        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(0, 15))
        self.assertEqual(leg.pickup_date, self.pickup_date)
        self.assertIsNone(leg.pickup_time_changed_at)

    def test_refusal_states_the_damage_the_time_only_match_would_do(self):
        leg = self._leg()
        payload = self._match(leg).json()
        # ~23h, not ~50 min — the number that should stop the dispatcher.
        self.assertIn("23h", payload["keep_date_shift"])
        self.assertIn("later", payload["keep_date_shift"])
        self.assertIn("earlier", payload["move_date_shift"])

    def test_confirm_move_date_moves_both_date_and_time(self):
        leg = self._leg()
        response = self._match(leg, confirm="move_date")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertTrue(response.json()["day_moved"])

        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, self.arrival_date)
        self.assertEqual(leg.pickup_time, time(23, 25))

    def test_confirm_keep_date_moves_time_only(self):
        """Still available — but only as a deliberate, recorded choice."""
        leg = self._leg()
        response = self._match(leg, confirm="keep_date")

        self.assertEqual(response.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, self.pickup_date)
        self.assertEqual(leg.pickup_time, time(23, 25))

    def test_same_day_match_needs_no_confirmation(self):
        """No new friction on the ordinary case."""
        leg = self._leg(
            pickup_time=time(10, 0), arrival_time=time(10, 40),
            arrival_date=self.pickup_date,
        )
        response = self._match(leg)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertFalse(response.json()["day_moved"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(10, 40))
        self.assertEqual(leg.pickup_date, self.pickup_date)

    def test_cancelled_flight_is_refused(self):
        leg = self._leg(arrival_date=self.pickup_date, status="Cancelled")
        response = self._match(leg)
        self.assertEqual(response.status_code, 409)
        self.assertIn("cancelled", response.json()["error"].lower())
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(0, 15))


class DayMoveStampingTests(_PickupFixture, TestCase):
    def test_day_move_stamps_pickup_date_was_and_raises_the_flag(self):
        leg = self._leg()
        apply_pickup_time_move(
            leg, time(23, 25), user=self.dispatcher,
            note="Flight match", new_date=self.arrival_date,
        )
        leg.refresh_from_db()

        self.assertEqual(leg.pickup_date_was, self.pickup_date)
        self.assertEqual(leg.pickup_time_was, time(0, 15))
        self.assertTrue(leg.has_unacked_time_change)
        self.assertTrue(leg.pickup_day_moved)

    def test_time_only_move_leaves_the_day_flag_down(self):
        leg = self._leg()
        apply_pickup_time_move(leg, time(1, 5), user=self.dispatcher)
        leg.refresh_from_db()

        self.assertEqual(leg.pickup_time_was, time(0, 15))
        self.assertIsNone(leg.pickup_date_was)
        self.assertTrue(leg.has_unacked_time_change)
        self.assertFalse(leg.pickup_day_moved)

    def test_audit_rows_written_for_both_fields(self):
        leg = self._leg()
        apply_pickup_time_move(
            leg, time(23, 25), user=self.dispatcher,
            note="Flight match", new_date=self.arrival_date,
        )
        # action="updated" excludes the unrelated "created" row the leg's own
        # post_save audit signal writes.
        fields = set(
            AuditLog.objects.filter(
                model_name="Leg", object_id=leg.id, action="updated"
            ).values_list("field_name", flat=True)
        )
        self.assertEqual(fields, {"pickup_time", "pickup_date"})

    def test_no_op_move_writes_nothing(self):
        leg = self._leg()
        self.assertFalse(apply_pickup_time_move(leg, time(0, 15), user=self.dispatcher))
        self.assertFalse(
            AuditLog.objects.filter(
                model_name="Leg", object_id=leg.id,
                field_name__in=["pickup_time", "pickup_date"],
            ).exists()
        )


class PickupMoveAttributionTests(_PickupFixture, TestCase):
    """The misattribution bug: a move must own its history row."""

    def _leg_history(self, leg):
        manager = get_history_manager_for_model(Leg)
        return list(
            manager.filter(id=leg.id).select_related("history_user")
            .order_by("history_date")
        )

    def test_move_writes_its_own_history_row_naming_the_dispatcher(self):
        leg = self._leg()
        apply_pickup_time_move(leg, time(1, 30), user=self.dispatcher)

        rows = self._leg_history(leg)
        latest = rows[-1]
        self.assertEqual(latest.pickup_time, time(1, 30))
        self.assertEqual(latest.history_user_id, self.dispatcher.id)
        self.assertEqual(latest.history_change_reason, "Flight match")

    def test_guest_move_is_not_pinned_on_a_staff_user(self):
        leg = self._leg()
        apply_pickup_time_move(
            leg, time(1, 30), user=None,
            note="Guest flight verification auto-adjust",
        )
        latest = self._leg_history(leg)[-1]
        self.assertIsNone(latest.history_user_id)

    def test_a_later_save_by_someone_else_does_not_absorb_the_move(self):
        """
        The regression that produced the incident's misleading log.

        A dispatcher moves the pickup; hours later a driver accepts the job.
        The driver's row must describe the status change only — the pickup move
        must not reappear inside it under the driver's name.
        """
        leg = self._leg()
        apply_pickup_time_move(leg, time(1, 30), user=self.dispatcher)

        leg.refresh_from_db()
        leg.status = "in-progress"
        leg._history_user = self.driver_user
        leg.save(update_fields=["status"])

        rows = self._leg_history(leg)
        driver_row = rows[-1]
        self.assertEqual(driver_row.history_user_id, self.driver_user.id)

        delta = driver_row.diff_against(rows[-2])
        changed = {c.field for c in delta.changes}
        self.assertIn("status", changed)
        self.assertNotIn("pickup_time", changed)
        self.assertNotIn("pickup_date", changed)


class LegTimelineTests(_PickupFixture, TestCase):
    def test_move_and_status_are_separate_events_with_the_right_actors(self):
        leg = self._leg()
        apply_pickup_time_move(
            leg, time(23, 25), user=self.dispatcher,
            note="Flight match", new_date=self.arrival_date,
        )
        leg.refresh_from_db()
        leg.status = "in-progress"
        leg._history_user = self.driver_user
        leg.save(update_fields=["status"])

        events = build_leg_timeline(leg)
        moves = [e for e in events if e["kind"] in ("pickup_moved", "flight_match")]
        statuses = [e for e in events if e["kind"] == "status"]

        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["actor"], "Iris Costa")
        self.assertIn("day_moved", moves[0]["flags"])
        self.assertEqual(moves[0]["severity"], "critical")

        self.assertTrue(statuses)
        self.assertEqual(statuses[0]["actor"], "Shelley")

        summary = timeline_summary(events)
        self.assertEqual(summary["day_moves"], 1)
        self.assertEqual(summary["late_recorded"], 0)

    def test_legacy_bundled_move_is_re_dated_and_de_attributed(self):
        """
        Reproduces a pre-fix row: the move was written with queryset.update()
        (no history row), then a driver's save swept it up six hours later.
        The timeline must put it back at its stamped time and refuse to credit
        the driver with it.
        """
        leg = self._leg()
        real_move_at = timezone.now() - timedelta(hours=6)

        # Exactly what the old write path did — no history row, stamps only.
        Leg.objects.filter(id=leg.id).update(
            pickup_time=time(23, 25),
            pickup_time_was=time(0, 15),
            pickup_time_changed_at=real_move_at,
            pickup_change_ack_at=None,
        )

        leg.refresh_from_db()
        leg.status = "in-progress"
        leg._history_user = self.driver_user
        leg.save(update_fields=["status"])

        events = build_leg_timeline(leg)
        moves = [e for e in events if e["kind"] in ("pickup_moved", "flight_match")]

        self.assertEqual(len(moves), 1)
        move = moves[0]
        self.assertIn("late_recorded", move["flags"])
        # Re-dated to when it really happened, not when it was noticed.
        self.assertAlmostEqual(
            move["at"].timestamp(), real_move_at.timestamp(), delta=5
        )
        # And explicitly NOT the driver.
        self.assertNotEqual(move["actor"], "Shelley")

    def test_audit_log_donates_the_name_history_never_had(self):
        """A legacy bundled move still has an AuditLog row naming the actor."""
        leg = self._leg()
        real_move_at = timezone.now() - timedelta(hours=6)
        Leg.objects.filter(id=leg.id).update(
            pickup_time=time(23, 25),
            pickup_time_was=time(0, 15),
            pickup_time_changed_at=real_move_at,
        )
        audit = AuditLog.objects.create(
            model_name="Leg", object_id=leg.id, action="updated",
            field_name="pickup_time", old_value="12:15 AM", new_value="11:25 PM",
            user=self.dispatcher, username=self.dispatcher.username,
            notes="Flight match",
        )
        AuditLog.objects.filter(pk=audit.pk).update(timestamp=real_move_at)

        leg.refresh_from_db()
        leg.status = "in-progress"
        leg._history_user = self.driver_user
        leg.save(update_fields=["status"])

        events = build_leg_timeline(leg)
        moves = [e for e in events if e["kind"] in ("pickup_moved", "flight_match")]
        self.assertEqual(len(moves), 1, "audit row must enrich, not duplicate")
        self.assertEqual(moves[0]["actor"], "Iris Costa")


class TimelineNoiseTests(_PickupFixture, TestCase):
    """Only the decision shows; the columns it triggers fold away."""

    def _assign(self, leg, driver):
        leg.driver = driver
        leg.driver_base_pay = Decimal("25.00")
        leg.driver_gratuity = Decimal("19.50")
        leg.driver_additional = Decimal("10.00")
        leg.driver_pay_amount = Decimal("54.50")
        leg.profit_estimate = Decimal("140.50")
        leg.driver_assigned_at = timezone.now()
        leg.driver_assigned_by = self.dispatcher
        leg._history_user = self.dispatcher
        leg.save()

    def _driver(self):
        from drivers.models import Driver
        profile = User.objects.create_user(
            username="david", password="pw",
            first_name="David", last_name="Encarnacion",
        )
        return Driver.objects.create(
            profile=profile, driver_type="inhouse", is_active=True
        )

    def test_assignment_is_one_event_not_seven_field_rows(self):
        leg = self._leg()
        self._assign(leg, self._driver())

        events = build_leg_timeline(leg)
        assigns = [e for e in events if e["kind"] == "driver_assigned"]
        self.assertEqual(len(assigns), 1)
        self.assertIn("David Encarnacion", assigns[0]["title"])
        # A first assignment is not a swap: simple_history's null-FK
        # placeholder must not leak into dispatcher-facing text.
        self.assertNotIn("Deleted driver", assigns[0]["title"])
        self.assertNotIn("pk=None", assigns[0]["title"])

        # One visible figure: the pay. Not base pay + gratuity + extra + profit.
        self.assertEqual(len(assigns[0]["details"]), 1)
        self.assertIn("54.50", assigns[0]["details"][0]["text"])

        # No separate "Details updated" row carrying the derived columns.
        self.assertFalse([e for e in events if e["kind"] == "field_change"])

    def test_the_derived_columns_are_folded_not_dropped(self):
        leg = self._leg()
        self._assign(leg, self._driver())

        assigns = [e for e in build_leg_timeline(leg) if e["kind"] == "driver_assigned"]
        folded = {d["label"] for d in assigns[0]["hidden_details"]}
        self.assertIn("Profit estimate", folded)
        self.assertIn("Driver gratuity", folded)

    def test_pay_edited_on_its_own_is_still_reported(self):
        """The gap the folding must not create."""
        leg = self._leg()
        self._assign(leg, self._driver())

        leg.refresh_from_db()
        leg.driver_gratuity = Decimal("40.00")
        leg.driver_pay_amount = Decimal("75.00")
        leg._history_user = self.dispatcher
        leg.save()

        pay_events = [e for e in build_leg_timeline(leg) if e["kind"] == "pay_changed"]
        self.assertEqual(len(pay_events), 1)
        self.assertEqual(pay_events[0]["severity"], "warn")

    def test_a_run_of_driver_status_taps_collapses_to_one_row(self):
        from reservations.models import LegStatus

        leg = self._leg()
        base = timezone.now()
        for offset, status in enumerate(
            ["on-the-way", "on-location", "picked-up", "completed"]
        ):
            row = LegStatus.objects.create(
                leg=leg, status=status, updated_by=self.driver_user,
            )
            LegStatus.objects.filter(pk=row.pk).update(
                timestamp=base + timedelta(minutes=10 * offset)
            )

        events = build_leg_timeline(leg)
        runs = [e for e in events if e["kind"] == "status_run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("on-the-way", runs[0]["title"])
        self.assertIn("completed", runs[0]["title"])
        # Every step is still listed underneath.
        self.assertEqual(len(runs[0]["details"]), 4)
        self.assertFalse([e for e in events if e["kind"] == "status"])

    def test_a_couple_of_status_taps_stay_expanded(self):
        from reservations.models import LegStatus

        leg = self._leg()
        LegStatus.objects.create(leg=leg, status="on-the-way", updated_by=self.driver_user)
        LegStatus.objects.create(leg=leg, status="completed", updated_by=self.driver_user)

        events = build_leg_timeline(leg)
        self.assertFalse([e for e in events if e["kind"] == "status_run"])
        self.assertEqual(len([e for e in events if e["kind"] == "status"]), 2)


class TimelineRenderTests(_PickupFixture, TestCase):
    """Template smoke tests — a timeline that 500s helps nobody."""

    def setUp(self):
        self.client.force_login(self.dispatcher)

    def _moved_leg(self):
        leg = self._leg()
        apply_pickup_time_move(
            leg, time(23, 25), user=self.dispatcher,
            note="Flight match", new_date=self.arrival_date,
        )
        return leg

    def test_full_page_renders_the_day_move_warning(self):
        leg = self._moved_leg()
        response = self.client.get(reverse("leg_history", args=[leg.id]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("calendar day", body)
        self.assertIn("Iris Costa", body)

    def test_modal_partial_renders(self):
        leg = self._moved_leg()
        response = self.client.get(reverse("leg_history_partial", args=[leg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Pickup date moved", response.content.decode())

    def test_pages_hosting_the_match_button_render_with_the_confirm_partial(self):
        """
        The shared matchFlightTime() client must actually reach the pages whose
        buttons now call it — otherwise the wrong-day 409 surfaces as a bare
        "could not update leg time" and the dispatcher never sees the choice.
        """
        self._moved_leg()
        for url_name in ("dashboard", "legs_list"):
            with self.subTest(page=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertIn("matchFlightTime", response.content.decode())
