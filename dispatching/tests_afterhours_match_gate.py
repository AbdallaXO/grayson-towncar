"""Tests for the after-hours fee gate on the flight-match action.

Run with:  ./manage.py test dispatching.tests_afterhours_match_gate

A match that lands the pickup in the 10 PM-6 AM window now stops and makes the
dispatcher decide the $20 before the move saves. Leg 36690 is why: the fee was
filed as a task instead, closed two minutes later with no note, re-raised five
more times over 39 hours, and the guest was never told.

Covers: the gate firing, each of the three answers, and the cases that must NOT
be interrupted (daytime match, fee already collected, money already in the price).
"""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ops.models import OperationalTask, StaffActivity
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Flight, Leg, Reservation
from reservations.signals import reservation_saved
from reservations.utils import AFTERHOURS_FEE_AMOUNT

PICKUP_DATE = date(2026, 11, 4)
LATE = time(22, 45)
DAYTIME = time(14, 30)


class AfterHoursMatchGateTests(TestCase):
    def setUp(self):
        post_save.disconnect(reservation_saved, sender=Reservation)
        self.addCleanup(
            lambda: post_save.connect(reservation_saved, sender=Reservation)
        )
        self.user = User.objects.create_user("luis", password="pw", is_staff=True)
        self.client.force_login(self.user)

        self.vehicle = Vehicle.objects.create(
            vehicle_type="mini_van", capacity=6, luggage_capacity=6
        )
        route = Route.objects.create(
            origin=Location.objects.create(name="Orlando International Airport"),
            destination=Location.objects.create(name="All WDW Disney Property Resorts"),
            inhouse_base_pay=Decimal("50.00"),
        )
        self.rate = Rate.objects.create(
            vehicle=self.vehicle, route=route,
            oneway_price=Decimal("120.00"), round_trip_price=Decimal("230.00"),
        )
        self.customer = Customer.objects.create(
            first_name="paul", last_name="iaffaldano",
            email="paul@example.com", phone_number="4075550142",
            card_brand="visa", card_last4="0540",
        )

    # ── fixtures ──────────────────────────────────────────────────────────
    def _leg(self, arrival_time, *, additional=Decimal("0.00"), marker=Decimal("0.00")):
        reservation = Reservation.objects.create(
            trip_type="round_trip", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("230.00"),
            additional_charges=additional,
            total_price=Decimal("230.00") + additional, status="confirmed",
        )
        arrival = datetime.combine(PICKUP_DATE, arrival_time)
        flight = Flight.objects.create(
            flight_type="arrival", airline="DL", flight_number="1423",
            scheduled_arrival_local=timezone.make_aware(arrival),
        )
        leg = Leg.objects.create(
            reservation=reservation, pickup_date=PICKUP_DATE,
            pickup_time=time(21, 30), pickup_location="Orlando International Airport",
            dropoff_location="Disney's Animal Kingdom Lodge",
            flight_information=flight, afterhours_fee=marker, status="confirmed",
        )
        return leg

    def _match(self, leg, **body):
        payload = {"leg_id": leg.id}
        payload.update(body)
        return self.client.post(
            reverse("match_leg_time_to_flight"),
            data=json.dumps(payload), content_type="application/json",
        )

    def _decide(self, leg, answer):
        """Answer the dialog the save raised. Separate call on purpose: the
        retime is already committed by the time this runs."""
        return self.client.post(
            reverse("afterhours_decision", args=[leg.id]),
            data=json.dumps({"answer": answer}), content_type="application/json",
        )

    # ── the gate ──────────────────────────────────────────────────────────
    def test_late_match_saves_then_asks(self):
        """The move lands; the $20 question comes after it, not instead of it."""
        leg = self._leg(LATE)
        r = self._match(leg)
        self.assertEqual(r.status_code, 200)
        d = r.json()["afterhours_prompt"]
        self.assertIsNotNone(d)
        self.assertEqual(d["fee_amount"], "20.00")
        self.assertEqual(d["current_total"], "230.00")
        self.assertEqual(d["new_total"], "250.00")
        self.assertTrue(d["has_card"])
        self.assertEqual(d["card_last4"], "0540")

    def test_the_pickup_moves_even_before_the_fee_is_answered(self):
        """A dispatcher must never lose a retime to a dialog they closed."""
        leg = self._leg(LATE)
        self._match(leg)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, LATE)

    def test_dismissing_the_dialog_leaves_the_fee_on_the_board(self):
        """Closing it is not the same as waiving it — the task stands."""
        leg = self._leg(LATE)
        self._match(leg)          # no decision posted, as if the dialog was closed
        self.assertTrue(
            OperationalTask.objects.filter(
                leg=leg, task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).exists()
        )

    def test_daytime_match_is_not_interrupted(self):
        leg = self._leg(DAYTIME)
        r = self._match(leg)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertIsNone(r.json()["afterhours_prompt"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, DAYTIME)

    def test_already_marked_is_not_interrupted(self):
        leg = self._leg(LATE, marker=AFTERHOURS_FEE_AMOUNT)
        self.assertIsNone(self._match(leg).json()["afterhours_prompt"])

    def test_fee_already_in_the_price_is_not_interrupted(self):
        """The booking itemised the $20, so there is nothing to ask about."""
        leg = self._leg(LATE, additional=AFTERHOURS_FEE_AMOUNT)
        self.assertIsNone(self._match(leg).json()["afterhours_prompt"])

    # ── the three answers ─────────────────────────────────────────────────
    @patch("dispatching.views._charge_afterhours_fee_for_leg")
    def test_charge_answer_charges_and_reports(self, charge):
        # Mirror what the real helper does on success — it writes the marker.
        # Without that the mock leaves the fee looking unpaid and the assertion
        # below would pass against a state production never reaches.
        def _charged(leg, user):
            leg.afterhours_fee = AFTERHOURS_FEE_AMOUNT
            leg.save(update_fields=["afterhours_fee"])
            return {"success": True}

        charge.side_effect = _charged
        leg = self._leg(LATE)
        self._match(leg)
        r = self._decide(leg, "charge")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(charge.called)
        self.assertEqual(r.json()["afterhours"]["action"], "charged")
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, LATE)
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        # Closing the backstop task is the real helper's job and it is mocked
        # here, so this asserts the marker instead — tests_gratuity_afterhours
        # covers the closing end for real.

    @patch("dispatching.views._charge_afterhours_fee_for_leg")
    def test_failed_charge_still_moves_the_pickup(self, charge):
        """A dead card must not cost us the pickup move — and must say so."""
        charge.return_value = {"success": False, "error": "No card on file to charge."}
        leg = self._leg(LATE)
        self._match(leg)
        r = self._decide(leg, "charge")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["afterhours"]["action"], "charge_failed")
        self.assertIn("No card on file", d["afterhours"]["message"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, LATE)

    def test_collected_answer_writes_the_marker(self):
        leg = self._leg(LATE)
        self._match(leg)
        r = self._decide(leg, "collected")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["afterhours"]["action"], "collected")
        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)

    def test_waive_answer_writes_the_marker_too(self):
        """Waiving has to stick, or it comes back tomorrow — the original bug."""
        leg = self._leg(LATE)
        self._match(leg)
        r = self._decide(leg, "waive")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["afterhours"]["action"], "waive")
        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)

    def test_answered_leg_raises_no_new_task(self):
        """The whole point: one decision, not six tasks."""
        leg = self._leg(LATE)
        self._match(leg); self._decide(leg, "waive")
        self.assertFalse(
            OperationalTask.objects.filter(
                leg=leg,
                task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).exists()
        )

    # ── attribution, and keeping it off the driver's screen ───────────────
    def test_settling_names_the_person_not_the_role(self):
        """'confirmed by dispatcher' is unattributable a week later."""
        self.user.first_name, self.user.last_name = "Iris", "Costa"
        self.user.save(update_fields=["first_name", "last_name"])
        leg = self._leg(LATE)
        self._match(leg); self._decide(leg, "collected")

        act = StaffActivity.objects.get(
            action_type=StaffActivity.ActionType.AFTERHOURS_SETTLED
        )
        self.assertEqual(act.user, self.user)
        self.assertIn("Iris Costa", act.metadata["reason"])
        self.assertEqual(act.metadata["settled_by"], "Iris Costa")
        self.assertEqual(act.metadata["leg_id"], leg.id)

    def test_username_is_used_when_there_is_no_full_name(self):
        leg = self._leg(LATE)
        self._match(leg); self._decide(leg, "waive")
        act = StaffActivity.objects.get(
            action_type=StaffActivity.ActionType.AFTERHOURS_SETTLED
        )
        self.assertEqual(act.metadata["settled_by"], "luis")
        self.assertIn("Waived by luis", act.metadata["reason"])

    def test_fee_note_never_reaches_the_driver_visible_notes(self):
        """private_notes renders on the driver's board, completed trips and
        weekly schedule. Our fee admin does not belong on their screen."""
        leg = self._leg(LATE)
        leg.private_notes = "$40.00 Gratuity Included"
        leg.save(update_fields=["private_notes"])

        self._match(leg); self._decide(leg, "waive")

        leg.refresh_from_db()
        # The gratuity note — the one money note a driver SHOULD see — survives.
        self.assertIn("Gratuity", leg.private_notes)
        for leaked in ("After-Hours", "after-hours", "Waived", "settled"):
            self.assertNotIn(leaked, leg.private_notes)

    @patch("dispatching.views._charge_afterhours_fee_for_leg", wraps=None)
    def test_charging_also_keeps_off_the_driver_notes(self, _charge):
        """Same rule on the charge path, which had its own note write."""
        _charge.side_effect = lambda leg, user: {"success": True}
        leg = self._leg(LATE)
        leg.private_notes = "$40.00 Gratuity Included"
        leg.save(update_fields=["private_notes"])
        self._match(leg); self._decide(leg, "charge")
        leg.refresh_from_db()
        self.assertNotIn("After-Hours", leg.private_notes)
        self.assertIn("Gratuity", leg.private_notes)

    def test_marker_change_is_attributed_in_the_leg_timeline(self):
        """The dispatcher-only trail that replaces the note."""
        leg = self._leg(LATE)
        self._match(leg); self._decide(leg, "waive")
        latest = leg.history.first()
        self.assertEqual(latest.history_user, self.user)
        self.assertEqual(latest.afterhours_fee, AFTERHOURS_FEE_AMOUNT)

    # ── the same gate on a hand-edited pickup ─────────────────────────────
    # update_leg_info is what the inline editor on the trip card posts to. It is
    # the commonest way a pickup lands after hours and, until this gate, the
    # quietest: it never called flag_afterhours_fee at all, so the time moved and
    # nothing anywhere said the $20 was now owed.
    def _edit(self, leg, **body):
        payload = {
            "leg_id": leg.id,
            "leg_data": {
                "pickup_date": leg.pickup_date.isoformat(),
                "pickup_time": leg.pickup_time.strftime("%H:%M"),
                "pickup_location": leg.pickup_location,
                "dropoff_location": leg.dropoff_location,
            },
        }
        payload["leg_data"].update(body.pop("leg_data", {}))
        payload.update(body)
        return self.client.post(
            reverse("update_leg_info"),
            data=json.dumps(payload), content_type="application/json",
        )

    def test_hand_edit_into_the_window_saves_then_asks(self):
        """The exact thing that saved silently: type 01:22 and save."""
        leg = self._leg(DAYTIME)
        r = self._edit(leg, leg_data={"pickup_time": "01:22"})
        self.assertEqual(r.status_code, 200)
        d = r.json()["afterhours_prompt"]
        self.assertIsNotNone(d)
        self.assertEqual(d["fee_amount"], "20.00")
        self.assertEqual(d["pickup_at"], "1:22 AM")

    def test_hand_edit_keeps_the_new_time_even_unanswered(self):
        """The edit is the dispatcher's intent; the fee is a follow-up."""
        leg = self._leg(DAYTIME)
        self._edit(leg, leg_data={"pickup_time": "01:22"})
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(1, 22))
        # ...and the money is on the board rather than lost.
        self.assertTrue(
            OperationalTask.objects.filter(
                leg=leg, task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).exists()
        )

    def test_hand_edit_to_daytime_is_not_gated(self):
        leg = self._leg(DAYTIME)
        r = self._edit(leg, leg_data={"pickup_time": "13:00"})
        self.assertEqual(r.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(13, 0))

    def test_editing_an_address_on_a_late_leg_does_not_nag(self):
        """The pickup has to MOVE. Otherwise every save re-asks and people
        learn to click through it without reading."""
        leg = self._leg(DAYTIME)
        leg.pickup_time = LATE
        leg.save(update_fields=["pickup_time"])
        r = self._edit(leg, leg_data={"dropoff_location": "Disney's Beach Club"})
        self.assertEqual(r.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.dropoff_location, "Disney's Beach Club")

    def test_hand_edit_waive_sticks(self):
        leg = self._leg(DAYTIME)
        self._edit(leg, leg_data={"pickup_time": "01:22"})
        r = self._decide(leg, "waive")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["afterhours"]["action"], "waive")
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_time, time(1, 22))
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)

    @patch("dispatching.views._charge_afterhours_fee_for_leg")
    def test_hand_edit_charge_reaches_the_charge_action(self, charge):
        charge.return_value = {"success": True}
        leg = self._leg(DAYTIME)
        self._edit(leg, leg_data={"pickup_time": "01:22"})
        r = self._decide(leg, "charge")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(charge.called)
        self.assertEqual(r.json()["afterhours"]["action"], "charged")

    def test_moving_back_out_of_the_window_closes_the_flag(self):
        """The backstop still runs when nothing is outstanding."""
        leg = self._leg(DAYTIME)
        leg.pickup_time = LATE
        leg.save(update_fields=["pickup_time"])
        from ops.tasks import flag_afterhours_fee
        flag_afterhours_fee(leg, LATE)
        self.assertTrue(
            OperationalTask.objects.filter(
                leg=leg, task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).exists()
        )

        self._edit(leg, leg_data={"pickup_time": "13:00"})

        self.assertFalse(
            OperationalTask.objects.filter(
                leg=leg, task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).exists()
        )

    # ── both gates at once ────────────────────────────────────────────────
    def test_wrong_day_is_asked_before_the_fee(self):
        """A delayed red-eye is on the wrong day AND after hours.

        The date question comes first because it decides which day the trip
        runs; answering it must not smuggle the money question past the
        dispatcher, and answering the money question must not discard the date
        answer. The client asks them in turn and accumulates both.
        """
        leg = self._leg(LATE)
        leg.pickup_date = PICKUP_DATE + timedelta(days=1)
        leg.save(update_fields=["pickup_date"])

        first = self._match(leg)
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json()["needs_confirmation"], "wrong_day")

        # Date answered: the move lands AND the fee question comes back with it.
        second = self._match(leg, confirm="move_date")
        self.assertEqual(second.status_code, 200)
        self.assertIsNotNone(second.json()["afterhours_prompt"])

        self._decide(leg, "waive")
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, PICKUP_DATE)
        self.assertEqual(leg.pickup_time, LATE)
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)


class FeeLogIsStaffOnlyTests(TestCase):
    """The fee trail gets its own block on the trip card — and only there.

    It used to live on leg.private_notes, which renders on the chauffeur's board,
    their completed trips and their weekly schedule. Gratuity is the only money
    note a driver should see.
    """

    def setUp(self):
        post_save.disconnect(reservation_saved, sender=Reservation)
        self.addCleanup(
            lambda: post_save.connect(reservation_saved, sender=Reservation)
        )
        self.staff = User.objects.create_user(
            "iris", password="pw", is_staff=True, first_name="Iris", last_name="Costa"
        )
        vehicle = Vehicle.objects.create(
            vehicle_type="mini_van", capacity=6, luggage_capacity=6
        )
        route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"),
        )
        rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("120.00"), round_trip_price=Decimal("230.00"),
        )
        customer = Customer.objects.create(
            first_name="paul", last_name="iaffaldano",
            email="paul@example.com", phone_number="4075550142",
        )
        self.reservation = Reservation.objects.create(
            trip_type="round_trip", customer=customer, rate=rate, vehicle=vehicle,
            base_price=Decimal("230.00"), total_price=Decimal("230.00"),
            status="confirmed",
        )
        self.leg = Leg.objects.create(
            reservation=self.reservation, pickup_date=PICKUP_DATE,
            pickup_time=LATE, pickup_location="MCO",
            dropoff_location="Disney", status="confirmed",
            private_notes="$40.00 Gratuity Included",
        )

    def _settle(self):
        from ops.tasks import settle_afterhours_fee
        settle_afterhours_fee(
            self.leg, settled_by=self.staff, note="Waived by Iris Costa — comped"
        )

    def test_the_trip_card_shows_the_fee_in_its_own_block(self):
        self._settle()
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("reservation_details", args=[str(self.reservation.uuid)])
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # "staff only" is the block's own badge text — the class names appear in
        # the stylesheet whether the block renders or not, so they prove nothing.
        self.assertIn("staff only", body)
        self.assertIn("After-hours fee", body)
        self.assertIn("Iris Costa", body)
        self.assertIn("comped", body)
        # The comment that introduced this block leaked onto the page, because
        # {# #} is single-line only. Pin it shut here as well as in the scanner.
        self.assertNotIn("Brings in window.askAfterHoursFee", body)
        self.assertNotIn("{% comment %}", body)

    def test_a_leg_with_nothing_to_say_shows_no_block(self):
        """Nothing settled AND nothing owed — the block stays away entirely.

        A late leg with no marker is now a standing "$20 not collected" warning,
        so this fixture has to be a daytime pickup to prove the quiet case.
        """
        self.leg.pickup_time = DAYTIME
        self.leg.save(update_fields=["pickup_time"])
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("reservation_details", args=[str(self.reservation.uuid)])
        )
        self.assertNotIn("staff only", resp.content.decode())

    def test_none_of_it_reaches_the_driver_visible_notes(self):
        self._settle()
        self.leg.refresh_from_db()
        self.assertIn("Gratuity", self.leg.private_notes)
        for leaked in ("comped", "Waived", "After-Hours", "after-hours"):
            self.assertNotIn(leaked, self.leg.private_notes)


class OutstandingFeeIsVisibleOnTheTripTests(TestCase):
    """Dismissing the dialog is allowed. Leaving no trace is not.

    Real case: a pickup was moved 6:10 PM -> 4:10 AM, the dialog was clicked away,
    the time saved correctly — and the trip card showed nothing. The task existed
    on the board, but the trip itself looked finished while $20 was uncollected.
    """

    def setUp(self):
        post_save.disconnect(reservation_saved, sender=Reservation)
        self.addCleanup(
            lambda: post_save.connect(reservation_saved, sender=Reservation)
        )
        self.staff = User.objects.create_user(
            "abdi", password="pw", is_staff=True, first_name="Abdalla", last_name="Aly"
        )
        self.client.force_login(self.staff)
        vehicle = Vehicle.objects.create(
            vehicle_type="mini_van", capacity=6, luggage_capacity=6
        )
        route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"),
        )
        rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("138.00"), round_trip_price=Decimal("276.00"),
        )
        customer = Customer.objects.create(
            first_name="paul", last_name="iaffaldano",
            email="paul@example.com", phone_number="4075550142",
        )
        self.reservation = Reservation.objects.create(
            trip_type="round_trip", customer=customer, rate=rate, vehicle=vehicle,
            base_price=Decimal("276.00"), total_price=Decimal("276.00"),
            additional_charges=Decimal("0.00"), status="confirmed",
        )
        self.leg = Leg.objects.create(
            reservation=self.reservation, pickup_date=PICKUP_DATE,
            pickup_time=time(18, 10), pickup_location="MCO",
            dropoff_location="Disney", status="in-progress",
        )

    def _card(self):
        return self.client.get(
            reverse("reservation_details", args=[str(self.reservation.uuid)])
        ).content.decode()

    def _move_to(self, hhmm):
        return self.client.post(
            reverse("update_leg_info"),
            data=json.dumps({
                "leg_id": self.leg.id,
                "leg_data": {
                    "pickup_date": self.leg.pickup_date.isoformat(),
                    "pickup_time": hhmm,
                    "pickup_location": self.leg.pickup_location,
                    "dropoff_location": self.leg.dropoff_location,
                },
            }),
            content_type="application/json",
        )

    def test_an_ignored_dialog_still_shows_on_the_trip(self):
        self._move_to("04:10")              # dialog dismissed: no decision posted
        body = self._card()
        self.assertIn("not collected", body)
        self.assertIn("$20.00 not collected", body)

    def test_it_names_who_moved_the_pickup_and_left_it(self):
        """The actual question: who changed the time and didn't charge."""
        self._move_to("04:10")
        self.assertIn("Abdalla Aly", self._card())

    def test_it_offers_a_way_back_into_the_dialog(self):
        self._move_to("04:10")
        body = self._card()
        self.assertIn("gt-fee-decide", body)
        self.assertIn("Decide now", body)

    def test_deciding_clears_the_warning(self):
        self._move_to("04:10")
        self.client.post(
            reverse("afterhours_decision", args=[self.leg.id]),
            data=json.dumps({"answer": "waive"}), content_type="application/json",
        )
        body = self._card()
        self.assertNotIn("not collected", body)
        self.assertIn("Waived by Abdalla Aly", body)

    def test_a_daytime_move_shows_nothing(self):
        self._move_to("13:30")
        self.assertNotIn("not collected", self._card())

    def test_the_warning_never_reaches_the_driver_notes(self):
        self._move_to("04:10")
        self.leg.refresh_from_db()
        self.assertNotIn("not collected", self.leg.private_notes or "")
