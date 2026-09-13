"""
Dispatch Shift System — Open / Close checklists.

The behaviours worth pinning: the counts are computed from the real board and
task data (never invented), a system row can never be ticked by hand, a
checklist cannot be finished while something is outstanding and unexplained,
and an unresolved note carries into the next shift and has to be taken by
somebody before the board is called safe.

Every test pins its own clock via explicit dates, the same way
ops/tests/test_timeclock.py does.
"""

from datetime import date, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from decimal import Decimal

from drivers.models import Driver
from ops import shift_checks, shift_services
from ops.models import (
    OperationalTask,
    ShiftChecklist,
    ShiftChecklistRow,
    ShiftException,
    ShiftSettings,
)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation


def _staff(username, first="", last=""):
    return User.objects.create_user(
        username=username, password="x", is_staff=True,
        first_name=first, last_name=last,
    )


class ShiftFixtureMixin:
    """A minimal but REAL board: a customer, a reservation and legs on a date."""

    def _base(self):
        if getattr(self, "_vehicle", None) is None:
            self._vehicle = Vehicle.objects.create(
                vehicle_type="towncar", capacity=3, luggage_capacity=3,
            )
            route = Route.objects.create(
                origin=Location.objects.create(name="MCO"),
                destination=Location.objects.create(name="Disney"),
                inhouse_base_pay=Decimal("50.00"),
            )
            self._rate = Rate.objects.create(
                vehicle=self._vehicle, route=route,
                oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
            )
            self._customer = Customer.objects.create(
                first_name="Ada", last_name="Byron",
                email="ada@example.com", phone_number="4070000000",
            )
        return self._vehicle, self._rate, self._customer

    def make_reservation(self):
        vehicle, rate, customer = self._base()
        return Reservation.objects.create(
            trip_type="one-way", customer=customer, rate=rate, vehicle=vehicle,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"),
            status="confirmed",
        )

    def make_driver(self, username, first, driver_type="inhouse"):
        user = User.objects.create_user(username=username, password="x", first_name=first)
        return Driver.objects.create(profile=user, driver_type=driver_type, is_active=True)

    def make_board(self, target_date, *, unassigned=0, unconfirmed_drivers=0,
                   confirmed=0):
        vehicle, _, _ = self._base()
        reservation = self.make_reservation()
        made, hour = [], 6

        def _leg(**kw):
            defaults = dict(
                reservation=reservation, pickup_date=target_date,
                pickup_location="MCO", dropoff_location="Disney", vehicle=vehicle,
            )
            defaults.update(kw)
            return Leg.objects.create(**defaults)

        for _ in range(unassigned):
            made.append(_leg(pickup_time=time(hour, 0)))
            hour += 1
        for i in range(unconfirmed_drivers):
            driver = self.make_driver(f"drv{i}{target_date:%d}", f"Driver{i}")
            for _ in range(2):  # two legs each, so grouping is exercised
                made.append(_leg(pickup_time=time(hour, 0), driver=driver,
                                 status="in-progress"))
                hour += 1
        for i in range(confirmed):
            driver = self.make_driver(f"ok{i}{target_date:%d}", f"Ok{i}")
            made.append(_leg(pickup_time=time(hour, 0), driver=driver,
                             status="confirmed"))
            hour += 1
        return reservation, made


class ChecksTests(ShiftFixtureMixin, TestCase):
    """The four counts read the real board, not a cache."""

    def setUp(self):
        self._vehicle = None
        self.day = date(2026, 9, 20)

    def test_unassigned_counts_only_driverless_legs(self):
        self.make_board(self.day, unassigned=3, confirmed=2)
        result = shift_checks.unassigned_legs(self.day)
        self.assertEqual(result.count, 3)
        self.assertFalse(result.is_clear)

    def test_unassigned_excludes_cancelled_legs_and_reservations(self):
        reservation, legs = self.make_board(self.day, unassigned=2)
        legs[0].status = "cancelled"
        legs[0].save(update_fields=["status"])
        self.assertEqual(shift_checks.unassigned_legs(self.day).count, 1)
        reservation.status = "cancelled"
        reservation.save(update_fields=["status"])
        self.assertEqual(shift_checks.unassigned_legs(self.day).count, 0)

    def test_unassigned_is_clear_on_an_empty_day(self):
        self.assertTrue(shift_checks.unassigned_legs(self.day).is_clear)

    def test_unconfirmed_groups_by_chauffeur_not_by_leg(self):
        # 2 chauffeurs × 2 legs each = 4 legs, but the count is 2 people.
        self.make_board(self.day, unconfirmed_drivers=2)
        result = shift_checks.unconfirmed_chauffeurs(self.day)
        self.assertEqual(result.count, 2)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0]["trips"], 2)
        # The board's own naming rule — not a values() read that yields blanks.
        self.assertNotIn("Unnamed", result.items[0]["who"])

    def test_only_the_chauffeurs_own_press_clears_them(self):
        """A dispatcher setting 'confirmed' from the board must NOT clear the row.

        Both write the same Leg.status; only LegStatus.updated_by tells them
        apart, which is the whole reason the check reads the history.
        """
        from reservations.models import LegStatus

        self.make_board(self.day, unconfirmed_drivers=1)
        legs = list(Leg.objects.filter(pickup_date=self.day, driver__isnull=False))
        self.assertEqual(shift_checks.unconfirmed_chauffeurs(self.day).count, 1)

        # The desk marks them confirmed. Still counts.
        desk = _staff("desk_user", "Desk")
        for leg in legs:
            leg.status = "confirmed"
            leg.save(update_fields=["status"])
            LegStatus.objects.create(leg=leg, status="confirmed", updated_by=desk)
        self.assertEqual(shift_checks.unconfirmed_chauffeurs(self.day).count, 1)

        # The chauffeur presses Accept Job himself. Now it clears.
        for leg in legs:
            LegStatus.objects.create(
                leg=leg, status="confirmed", updated_by=leg.driver.profile,
            )
        self.assertEqual(shift_checks.unconfirmed_chauffeurs(self.day).count, 0)

    def test_an_affiliate_clears_by_accepting_in_their_own_portal(self):
        """Affiliates never touch the in-house status ladder — their acceptance
        is `Leg.operator_accepted_at`."""
        vehicle, _, _ = self._base()
        reservation = self.make_reservation()
        affiliate = self.make_driver("aff1", "Affiliate", driver_type="affiliate")
        leg = Leg.objects.create(
            reservation=reservation, pickup_date=self.day, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", vehicle=vehicle,
            driver=affiliate, status="in-progress",
        )
        self.assertEqual(shift_checks.unconfirmed_chauffeurs(self.day).count, 1)

        leg.operator_accepted_at = timezone.now()
        leg.save(update_fields=["operator_accepted_at"])
        self.assertEqual(shift_checks.unconfirmed_chauffeurs(self.day).count, 0)

    def test_the_flight_count_is_mismatches_only(self):
        """Pickup-time acks are a DIFFERENT job and must not inflate the red
        number — they ride along as a grey aside with one bulk action."""
        _, legs = self.make_board(self.day, unassigned=2)
        OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            title="Flight mismatch: Ada Byron", leg=legs[0],
            due_at=timezone.now(),
        )
        result = shift_checks.unreviewed_flight_alerts(self.day)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.aside_count, 0)

        moved = legs[1]
        moved.pickup_time_changed_at = timezone.now()
        moved.pickup_change_ack_at = None
        moved.save(update_fields=["pickup_time_changed_at", "pickup_change_ack_at"])
        result = shift_checks.unreviewed_flight_alerts(self.day)
        self.assertEqual(result.count, 1, "an ack is not a mismatch")
        self.assertEqual(result.aside_count, 1)
        self.assertIn("acknowledge", result.aside)

        moved.pickup_change_ack_at = timezone.now()
        moved.save(update_fields=["pickup_change_ack_at"])
        self.assertEqual(shift_checks.unreviewed_flight_alerts(self.day).aside_count, 0)

    def test_a_completed_flight_task_stops_counting(self):
        _, legs = self.make_board(self.day, unassigned=1)
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            title="Flight mismatch", leg=legs[0], due_at=timezone.now(),
        )
        self.assertEqual(shift_checks.unreviewed_flight_alerts(self.day).count, 1)
        task.status = OperationalTask.Status.COMPLETED
        task.save(update_fields=["status"])
        self.assertEqual(shift_checks.unreviewed_flight_alerts(self.day).count, 0)

    def test_conflict_row_counts_both_conflict_and_tight_turn(self):
        _, legs = self.make_board(self.day, unassigned=2)
        OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            title="Driver Conflict — George", leg=legs[0], due_at=timezone.now(),
        )
        OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.TIGHT_TURN,
            title="Tight turn — Iris", leg=legs[1], due_at=timezone.now(),
        )
        self.assertEqual(shift_checks.open_conflict_tasks(self.day).count, 2)

    def test_a_broken_check_never_takes_the_checklist_down(self):
        results = shift_checks.run_checks(["unassigned", "not_a_real_check"], self.day)
        self.assertIn("unassigned", results)
        self.assertNotIn("not_a_real_check", results)


class ChecklistRowTests(ShiftFixtureMixin, TestCase):
    """Opening builds the right rows; a system row can never be ticked."""

    def setUp(self):
        self._vehicle = None
        self.user = _staff("bryan", "Bryan")
        self.day = date(2026, 9, 20)

    def test_open_shift_runs_the_sop_order(self):
        # Steps 1-4 of the Opener SOP, in the order it says to run them.
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        keys = [r.key for r in checklist.rows.filter(kind="system")]
        # Step 4 on the open asks what the NEW TIMES broke, not how many
        # conflict tasks are standing — see ops/move_impact.py.
        self.assertEqual(keys, ["unassigned", "unconfirmed", "flight", "moves"])

    def test_the_human_rows_put_the_inboxes_last(self):
        # Systems up and the triage post first; texts, email, GoHighLevel last,
        # in that order. "Protect today first."
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        keys = [r.key for r in checklist.rows.filter(kind="human")]
        self.assertEqual(keys, ["phone", "triage", "sms", "email", "ghl"])

    def test_the_close_ends_on_the_handover_not_the_opening_post(self):
        """The WhatsApp row is a different job at each end of the day.

        Open and close shared one human-row list, so the close checklist asked
        a dispatcher going home to send the OPENING message. At the open the
        post comes first because it tells you what you are walking into; at the
        close it comes last because it reports what you are handing over.
        """
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.CLOSE, user=self.user,
        )
        keys = [r.key for r in checklist.rows.filter(kind="human")]
        self.assertEqual(keys, ["phone_close", "sms", "email", "ghl", "handover"])
        self.assertNotIn("triage", keys)

    def test_the_close_does_not_ask_for_the_ringer_again(self):
        """The opener set the ringer hours ago.

        Asking a second time teaches people to tick past a row they already
        did, which is how a checklist stops being read at all.
        """
        labels = [
            r.label for r in shift_services.get_or_open_checklist(
                self.day, ShiftChecklist.Kind.CLOSE, user=self.user,
            ).rows.filter(kind="human")
        ]
        self.assertIn("RingCentral logged in and working", labels)
        self.assertNotIn("RingCentral logged in, ringer on", labels)

    def test_close_shift_adds_the_conflict_row(self):
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.CLOSE, user=self.user,
        )
        keys = [r.key for r in checklist.rows.filter(kind="system")]
        self.assertIn("conflicts", keys)

    def test_close_shift_looks_at_tomorrow(self):
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.CLOSE, user=self.user,
        )
        self.assertEqual(checklist.target_date, self.day + timedelta(days=1))

    def test_open_shift_looks_at_its_own_day(self):
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        self.assertEqual(checklist.target_date, self.day)

    def test_opening_twice_returns_the_same_checklist(self):
        first = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        second = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ShiftChecklist.objects.count(), 1)

    def test_a_system_row_cannot_be_ticked_by_hand(self):
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        with self.assertRaises(shift_services.ShiftError):
            shift_services.confirm_human_row(checklist, "unassigned", self.user)

    def test_a_human_row_confirms_and_records_who(self):
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        row = shift_services.confirm_human_row(checklist, "sms", self.user)
        self.assertEqual(row.state, ShiftChecklistRow.State.CLEAR)
        self.assertEqual(row.confirmed_by, self.user)
        self.assertIsNotNone(row.confirmed_at)

    def test_system_rows_go_green_on_their_own(self):
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        shift_services.refresh_system_rows(checklist)
        row = checklist.rows.get(key="unassigned")
        self.assertEqual(row.state, ShiftChecklistRow.State.CLEAR)

        self.make_board(self.day, unassigned=2)
        shift_services.refresh_system_rows(checklist)
        row.refresh_from_db()
        self.assertEqual(row.state, ShiftChecklistRow.State.OPEN)
        self.assertEqual(row.last_count, 2)


class GateTests(ShiftFixtureMixin, TestCase):
    """No silent exceptions: nothing finishes while something is unexplained."""

    def setUp(self):
        self._vehicle = None
        self.user = _staff("iris", "Iris")
        self.owner = _staff("luis", "Luis")
        self.day = date(2026, 9, 20)
        self.checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )

    def _clear_human_rows(self):
        for row in self.checklist.rows.filter(kind="human"):
            shift_services.confirm_human_row(self.checklist, row.key, self.user)

    def test_cannot_finish_while_a_count_is_not_zero(self):
        self.make_board(self.day, unassigned=1)
        self._clear_human_rows()
        with self.assertRaises(shift_services.ShiftError):
            shift_services.complete_checklist(self.checklist, self.user)

    def test_cannot_finish_while_a_human_row_is_untouched(self):
        with self.assertRaises(shift_services.ShiftError):
            shift_services.complete_checklist(self.checklist, self.user)

    def test_writing_it_up_unblocks_the_finish(self):
        self.make_board(self.day, unassigned=1)
        self._clear_human_rows()
        shift_services.document_exception(
            self.checklist,
            what="One trip still needs a chauffeur.",
            owner=self.owner,
            next_action="Call Carlos at 9 AM.",
            user=self.user,
            row_key="unassigned",
        )
        shift_services.complete_checklist(self.checklist, self.user)
        self.checklist.refresh_from_db()
        self.assertTrue(self.checklist.is_complete)
        self.assertEqual(self.checklist.completed_by, self.user)

    def test_an_exception_needs_all_three_parts(self):
        for kwargs in (
            {"what": "", "next_action": "Call him."},
            {"what": "Something", "next_action": ""},
        ):
            with self.assertRaises(shift_services.ShiftError):
                shift_services.document_exception(
                    self.checklist, owner=self.owner, user=self.user, **kwargs,
                )
        with self.assertRaises(shift_services.ShiftError):
            shift_services.document_exception(
                self.checklist, what="Something", owner=None,
                next_action="Call him.", user=self.user,
            )

    def test_a_clean_day_finishes_and_snapshots_the_counts(self):
        self._clear_human_rows()
        shift_services.complete_checklist(self.checklist, self.user)
        self.checklist.refresh_from_db()
        self.assertTrue(self.checklist.is_complete)
        self.assertEqual(self.checklist.counts_snapshot.get("unassigned"), 0)

    def test_board_safe_ignores_the_human_rows(self):
        # Board safe is about the board, not about the inbox.
        shift_services.mark_board_safe(self.checklist, self.user)
        self.checklist.refresh_from_db()
        self.assertIsNotNone(self.checklist.board_safe_at)

    def test_board_safe_refuses_while_a_count_stands_unexplained(self):
        self.make_board(self.day, unassigned=1)
        shift_services.refresh_system_rows(self.checklist)
        with self.assertRaises(shift_services.ShiftError):
            shift_services.mark_board_safe(self.checklist, self.user)

    def test_gate_times_are_frozen_when_the_checklist_opens(self):
        self.assertEqual(self.checklist.targets.get("board_safe"), "07:15")
        cfg = ShiftSettings.load()
        cfg.board_safe_target = time(6, 0)
        cfg.save()
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.targets.get("board_safe"), "07:15")

    def test_on_time_and_late_are_judged_against_the_frozen_target(self):
        tz = timezone.get_current_timezone()
        early = timezone.make_aware(
            timezone.datetime.combine(self.day, time(7, 2)), tz,
        )
        self.checklist.board_safe_at = early
        self.assertEqual(self.checklist.board_safe_status, "on_time")
        self.checklist.board_safe_at = timezone.make_aware(
            timezone.datetime.combine(self.day, time(7, 40)), tz,
        )
        self.assertEqual(self.checklist.board_safe_status, "late")

    def test_reopening_puts_it_back_in_play(self):
        self._clear_human_rows()
        shift_services.complete_checklist(self.checklist, self.user)
        shift_services.reopen_checklist(self.checklist, self.owner)
        self.checklist.refresh_from_db()
        self.assertFalse(self.checklist.is_complete)
        self.assertEqual(self.checklist.reopen_count, 1)
        self.assertEqual(self.checklist.reopened_by, self.owner)


class CarryForwardTests(TestCase):
    """A leftover follows the shift chain until somebody sorts it."""

    def setUp(self):
        self.user = _staff("bryan2", "Bryan")
        self.owner = _staff("luis2", "Luis")
        self.day = date(2026, 9, 20)

    def _close_with_exception(self, day):
        close = shift_services.get_or_open_checklist(
            day, ShiftChecklist.Kind.CLOSE, user=self.user,
        )
        exception = shift_services.document_exception(
            close,
            what="Michael has not confirmed his 6:00 AM pickup.",
            owner=self.owner,
            next_action="Call him at 5:30 AM.",
            user=self.user,
        )
        return close, exception

    def test_an_unresolved_note_appears_on_the_next_open(self):
        _, original = self._close_with_exception(self.day)
        nxt = shift_services.get_or_open_checklist(
            self.day + timedelta(days=1), ShiftChecklist.Kind.OPEN, user=self.user,
        )
        carried = list(nxt.exceptions.all())
        self.assertEqual(len(carried), 1)
        self.assertEqual(carried[0].carried_from_id, original.id)
        self.assertEqual(carried[0].owner, self.owner)

    def test_a_resolved_note_does_not_carry(self):
        _, original = self._close_with_exception(self.day)
        shift_services.resolve_exception(original, self.user)
        nxt = shift_services.get_or_open_checklist(
            self.day + timedelta(days=1), ShiftChecklist.Kind.OPEN, user=self.user,
        )
        self.assertEqual(nxt.exceptions.count(), 0)

    def test_a_carried_note_blocks_board_safe_until_someone_takes_it(self):
        self._close_with_exception(self.day)
        nxt = shift_services.get_or_open_checklist(
            self.day + timedelta(days=1), ShiftChecklist.Kind.OPEN, user=self.user,
        )
        with self.assertRaises(shift_services.ShiftError):
            shift_services.mark_board_safe(nxt, self.user)

        carried = nxt.exceptions.first()
        shift_services.acknowledge_exception(carried, self.user)
        shift_services.mark_board_safe(nxt, self.user)
        nxt.refresh_from_db()
        self.assertIsNotNone(nxt.board_safe_at)

    def test_carry_depth_counts_the_shifts_it_has_survived(self):
        _, original = self._close_with_exception(self.day)
        nxt = shift_services.get_or_open_checklist(
            self.day + timedelta(days=1), ShiftChecklist.Kind.OPEN, user=self.user,
        )
        first_carry = nxt.exceptions.first()
        self.assertEqual(first_carry.carry_depth, 1)


class SummaryTests(TestCase):
    """The message a person pastes into WhatsApp — no bot, no jargon."""

    def setUp(self):
        self.user = _staff("bryan3", "Bryan", "Nolan")
        self.owner = _staff("luis3", "Luis", "Ortiz")
        self.day = date(2026, 9, 20)
        self.checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        for row in self.checklist.rows.filter(kind="human"):
            shift_services.confirm_human_row(self.checklist, row.key, self.user)

    def test_a_clean_shift_says_nothing_is_outstanding(self):
        shift_services.complete_checklist(self.checklist, self.user)
        summary = shift_services.build_summary(self.checklist)
        self.assertIn("Bryan Nolan", summary)
        self.assertIn("Nothing outstanding", summary)

    def test_the_summary_names_the_person_and_the_next_step(self):
        shift_services.document_exception(
            self.checklist,
            what="Michael has not confirmed his 6:00 AM pickup",
            owner=self.owner,
            next_action="call him at 5:30 AM",
            user=self.user,
        )
        shift_services.complete_checklist(self.checklist, self.user)
        summary = shift_services.build_summary(self.checklist)
        self.assertIn("Michael has not confirmed", summary)
        self.assertIn("Luis Ortiz", summary)
        self.assertIn("call him at 5:30 AM", summary)

    def test_the_summary_avoids_field_and_file_names(self):
        shift_services.complete_checklist(self.checklist, self.user)
        summary = shift_services.build_summary(self.checklist).lower()
        for jargon in ("checklist_id", "task_type", "leg_id", "queryset", ".py"):
            self.assertNotIn(jargon, summary)


class ViewTests(TestCase):
    """Access, the soft-error contract, and the Lead's gate."""

    def setUp(self):
        self.user = _staff("dispatcher", "Dee")
        self.lead = _staff("lead", "Lee")
        self.day = timezone.localdate()

    def test_open_shift_needs_staff(self):
        self.client.logout()
        response = self.client.get(reverse("shift_open"))
        self.assertEqual(response.status_code, 302)

    def test_open_shift_loads_for_a_dispatcher(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("shift_open"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open Shift")

    def test_close_shift_loads_and_looks_at_tomorrow(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("shift_close"))
        self.assertEqual(response.status_code, 200)
        checklist = ShiftChecklist.objects.get(kind=ShiftChecklist.Kind.CLOSE)
        self.assertEqual(checklist.target_date, self.day + timedelta(days=1))

    def test_the_close_page_is_written_for_the_end_of_the_day(self):
        """Standing order, rows and wording all belong to the shift you are on.

        The page was serving the OPEN copy on both: "a 7 AM pickup can't" as
        the standing order, and an "Opening message sent" row for a dispatcher
        going home.
        """
        self.client.force_login(self.user)
        html = self.client.get(reverse("shift_close")).content.decode()
        self.assertIn("Leave it clean for whoever opens", html)
        self.assertNotIn("a 7 AM pickup can&#x27;t", html)
        self.assertIn("Closing message sent on WhatsApp", html)
        self.assertNotIn("Opening message sent on WhatsApp", html)

    def test_the_open_page_keeps_its_own_standing_order(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("shift_open")).content.decode()
        self.assertIn("Protect today first", html)
        self.assertIn("Opening message sent on WhatsApp", html)
        self.assertNotIn("Closing message sent on WhatsApp", html)

    def test_a_bad_action_is_a_soft_error_not_a_500(self):
        self.client.force_login(self.user)
        self.client.get(reverse("shift_open"))
        checklist = ShiftChecklist.objects.get(kind=ShiftChecklist.Kind.OPEN)
        response = self.client.post(
            reverse("shift_action"),
            data={"action": "confirm_row", "key": "unassigned",
                  "checklist_id": checklist.id},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])

    def test_confirming_a_human_row_through_the_endpoint(self):
        self.client.force_login(self.user)
        self.client.get(reverse("shift_open"))
        checklist = ShiftChecklist.objects.get(kind=ShiftChecklist.Kind.OPEN)
        response = self.client.post(
            reverse("shift_action"),
            data={"action": "confirm_row", "key": "sms", "checklist_id": checklist.id},
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"])
        self.assertEqual(
            checklist.rows.get(key="sms").state, ShiftChecklistRow.State.CLEAR,
        )

    def test_the_lead_view_is_closed_to_an_ordinary_dispatcher(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("shift_lead"))
        self.assertEqual(response.status_code, 302)

    def test_the_lead_view_opens_for_a_superuser(self):
        admin = User.objects.create_superuser("boss", "b@example.com", "x")
        self.client.force_login(admin)
        response = self.client.get(reverse("shift_lead"))
        self.assertEqual(response.status_code, 200)

    def test_the_lead_view_opens_with_the_permission(self):
        from django.contrib.auth.models import Permission
        self.lead.user_permissions.add(
            Permission.objects.get(codename="review_checklists"),
        )
        self.client.force_login(User.objects.get(pk=self.lead.pk))
        response = self.client.get(reverse("shift_lead"))
        self.assertEqual(response.status_code, 200)

    def test_an_ordinary_dispatcher_cannot_reopen(self):
        self.client.force_login(self.user)
        self.client.get(reverse("shift_open"))
        checklist = ShiftChecklist.objects.get(kind=ShiftChecklist.Kind.OPEN)
        for row in checklist.rows.filter(kind="human"):
            shift_services.confirm_human_row(checklist, row.key, self.user)
        shift_services.complete_checklist(checklist, self.user)
        response = self.client.post(
            reverse("shift_action"),
            data={"action": "reopen", "checklist_id": checklist.id},
            content_type="application/json",
        )
        self.assertFalse(response.json()["success"])


class BoundaryTests(ShiftFixtureMixin, TestCase):
    """The shift layer reads the dispatch system. It must never write to it."""

    def setUp(self):
        self._vehicle = None
        self.user = _staff("reader", "Rita")
        self.day = date(2026, 9, 20)

    def test_running_the_checks_changes_no_leg(self):
        _, legs = self.make_board(self.day, unassigned=2, unconfirmed_drivers=1)
        before = [
            (leg.pk, leg.driver_id, leg.status, leg.pickup_time) for leg in legs
        ]
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )
        shift_services.refresh_system_rows(checklist)
        after = [
            (leg.pk, leg.driver_id, leg.status, leg.pickup_time)
            for leg in Leg.objects.filter(pk__in=[l.pk for l in legs]).order_by("pk")
        ]
        self.assertEqual(sorted(before), sorted(after))

    def test_completing_a_checklist_closes_no_task(self):
        _, legs = self.make_board(self.day, unassigned=1)
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            title="Driver Conflict — George", leg=legs[0], due_at=timezone.now(),
        )
        checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.CLOSE, user=self.user,
        )
        for row in checklist.rows.filter(kind="human"):
            shift_services.confirm_human_row(checklist, row.key, self.user)
        shift_services.document_exception(
            checklist, what="Still one to cover.", owner=self.user,
            next_action="Cover it in the morning.", user=self.user,
            row_key="unassigned",
        )
        task.refresh_from_db()
        self.assertEqual(task.status, OperationalTask.Status.PENDING)


class OpenerWorkflowTests(TestCase):
    """Clocking in should hand the opener their list, and nobody else's."""

    def setUp(self):
        from ops.models import StaffWeeklySchedule
        self.today = timezone.localdate()
        self.dow = self.today.weekday()

        self.opener = _staff("opener_u", "Abdalla")
        self.other = _staff("other_u", "Iris")

        # Abdalla is explicitly the opener; Iris is in earlier but is not.
        #
        # Both windows span the whole day on purpose: a punch outside someone's
        # hours becomes an approval request rather than a clock-in (by design,
        # ops/services.py::clock_in_or_request), and these tests are about the
        # opener hand-off, not about the schedule gate. Pinning the window keeps
        # them from passing or failing on the hour the suite happens to run.
        StaffWeeklySchedule.objects.create(
            user=self.opener, day_of_week=self.dow, is_working=True,
            start_time=time(0, 30), end_time=time(23, 59), role="opener",
        )
        StaffWeeklySchedule.objects.create(
            user=self.other, day_of_week=self.dow, is_working=True,
            start_time=time(0, 0), end_time=time(23, 59),
        )

    def test_the_assigned_opener_beats_the_earliest_person_in(self):
        opener = shift_services.scheduled_opener(self.today)
        self.assertEqual(opener, self.opener)

    def test_the_opener_is_pointed_at_their_list(self):
        url = shift_services.opening_prompt_for(self.opener, self.today)
        self.assertIsNotNone(url)
        self.assertIn("/shift/open/", url)

    def test_nobody_else_is_interrupted(self):
        self.assertIsNone(shift_services.opening_prompt_for(self.other, self.today))

    def test_a_finished_opening_stops_pointing_anyone_anywhere(self):
        checklist = shift_services.get_or_open_checklist(
            self.today, ShiftChecklist.Kind.OPEN, user=self.opener,
        )
        for row in checklist.rows.filter(kind="human"):
            shift_services.confirm_human_row(checklist, row.key, self.opener)
        shift_services.complete_checklist(checklist, self.opener)
        self.assertIsNone(shift_services.opening_prompt_for(self.opener, self.today))

    def test_clocking_in_hands_back_the_opening_link(self):
        self.client.force_login(self.opener)
        response = self.client.post(
            reverse("timeclock_action"),
            data={"action": "clock_in"},
            content_type="application/json",
        )
        body = response.json()
        self.assertTrue(body["success"])
        self.assertIn("/shift/open/", body.get("opening_url", ""))

    def test_clocking_in_hands_nothing_back_to_anyone_else(self):
        self.client.force_login(self.other)
        response = self.client.post(
            reverse("timeclock_action"),
            data={"action": "clock_in"},
            content_type="application/json",
        )
        self.assertEqual(response.json().get("opening_url", ""), "")

    def test_a_broken_roster_never_costs_someone_their_punch(self):
        from unittest.mock import patch
        self.client.force_login(self.opener)
        with patch("ops.shift_services.scheduled_opener", side_effect=RuntimeError("boom")):
            response = self.client.post(
                reverse("timeclock_action"),
                data={"action": "clock_in"},
                content_type="application/json",
            )
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json().get("opening_url", ""), "")

    def test_the_page_says_who_is_down_to_open(self):
        self.client.force_login(self.opener)
        response = self.client.get(reverse("shift_open"))
        # Literal template text is NOT html-escaped — assert the plain words.
        self.assertContains(response, "the opener")

    def test_someone_else_is_told_whose_list_it_is(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("shift_open"))
        self.assertContains(response, "Abdalla")
        self.assertContains(response, "is the opener")

    def test_the_footer_offers_one_complete_button(self):
        self.client.force_login(self.opener)
        response = self.client.get(reverse("shift_open"))
        self.assertContains(response, "Complete open")
        self.assertContains(response, 'data-shift-action="complete"')

    def test_signing_records_who_signed_not_who_was_rostered(self):
        checklist = shift_services.get_or_open_checklist(
            self.today, ShiftChecklist.Kind.OPEN, user=self.opener,
        )
        for row in checklist.rows.filter(kind="human"):
            shift_services.confirm_human_row(checklist, row.key, self.other)
        shift_services.complete_checklist(checklist, self.other)
        checklist.refresh_from_db()
        self.assertEqual(checklist.completed_by, self.other)


class PageScriptTests(TestCase):
    """The checklist's own JavaScript has to survive the base template's load order.

    Bootstrap's bundle is loaded at the FOOT of main.html, after the content
    block. Building a `bootstrap.Modal` at script-parse time therefore throws a
    ReferenceError that silently kills every handler defined after it — which is
    how "Write it up" came to do nothing at all on the first build.
    """

    def setUp(self):
        self.user = _staff("scripted", "Sam")
        self.client.force_login(self.user)

    def test_the_script_waits_for_the_page_to_load(self):
        html = self.client.get(reverse("shift_open")).content.decode()
        self.assertIn("DOMContentLoaded", html)

    def test_no_bootstrap_component_is_built_at_parse_time(self):
        html = self.client.get(reverse("shift_open")).content.decode()
        self.assertNotIn("new bootstrap.", html)

    def test_bootstrap_really_does_load_after_our_script(self):
        # If this ever stops being true the guard above is redundant rather than
        # wrong — but it is the reason the guard exists, so pin it.
        html = self.client.get(reverse("shift_open")).content.decode()
        self.assertLess(
            html.index("DOMContentLoaded"),
            html.index("bootstrap.bundle.min.js"),
        )

    def test_the_note_button_and_its_inline_form_are_both_rendered(self):
        # A human row is always notable, so its form is always on the page.
        html = self.client.get(reverse("shift_open")).content.decode()
        self.assertIn("data-note-for=", html)
        self.assertIn("data-form-for=", html)
        self.assertIn("data-note-save=", html)
        self.assertIn(">Note<", html)

    def test_writing_one_up_through_the_endpoint_works(self):
        self.client.get(reverse("shift_open"))
        checklist = ShiftChecklist.objects.get(kind=ShiftChecklist.Kind.OPEN)
        response = self.client.post(
            reverse("shift_action"),
            data={
                "action": "document", "key": "sms",
                "what": "Three texts still unanswered from overnight.",
                "owner_id": self.user.id,
                "next_action": "Clear them before 9 AM.",
                "checklist_id": checklist.id,
            },
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"])
        self.assertEqual(checklist.exceptions.count(), 1)
        self.assertEqual(
            checklist.rows.get(key="sms").state, ShiftChecklistRow.State.DOCUMENTED,
        )


class PastPickupTests(ShiftFixtureMixin, TestCase):
    """A count is work you can still DO something about.

    A trip whose pickup time has gone is a post-mortem, not a pending action, so
    it is reported on a grey line instead of making the gate unreachable.
    """

    def setUp(self):
        self._vehicle = None
        self.today = timezone.localdate()
        self.tomorrow = self.today + timedelta(days=1)

    def _at(self, hour, minute=0):
        return time(hour, minute)

    def test_past_pickups_are_reported_not_counted(self):
        vehicle, _, _ = self._base()
        reservation = self.make_reservation()
        now = timezone.localtime(timezone.now()).time()
        if now.hour < 2 or now.hour > 21:
            self.skipTest("needs an hour with room either side")

        # one gone, one still ahead
        for hh in (now.hour - 1, now.hour + 1):
            Leg.objects.create(
                reservation=reservation, pickup_date=self.today,
                pickup_time=self._at(hh), pickup_location="MCO",
                dropoff_location="Disney", vehicle=vehicle,
            )
        result = shift_checks.unassigned_legs(self.today)
        self.assertEqual(result.count, 1, "only the one still ahead counts")
        self.assertEqual(result.aside_count, 1)
        self.assertIn("already past pickup", result.aside)

    def test_tomorrow_has_nothing_past_so_no_grey_line(self):
        vehicle, _, _ = self._base()
        reservation = self.make_reservation()
        for hh in (5, 22):
            Leg.objects.create(
                reservation=reservation, pickup_date=self.tomorrow,
                pickup_time=self._at(hh), pickup_location="MCO",
                dropoff_location="Disney", vehicle=vehicle,
            )
        result = shift_checks.unassigned_legs(self.tomorrow)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.aside_count, 0)
        self.assertEqual(result.aside, "")

    def test_past_pickups_are_dropped_from_unconfirmed_too(self):
        vehicle, _, _ = self._base()
        reservation = self.make_reservation()
        now = timezone.localtime(timezone.now()).time()
        if now.hour < 2 or now.hour > 21:
            self.skipTest("needs an hour with room either side")
        driver = self.make_driver("past_drv", "Past")
        for hh in (now.hour - 1, now.hour + 1):
            Leg.objects.create(
                reservation=reservation, pickup_date=self.today,
                pickup_time=self._at(hh), pickup_location="MCO",
                dropoff_location="Disney", vehicle=vehicle,
                driver=driver, status="in-progress",
            )
        result = shift_checks.unconfirmed_chauffeurs(self.today)
        self.assertEqual(result.count, 1, "the chauffeur still has one ahead")
        self.assertEqual(result.items[0]["trips"], 1)
        self.assertEqual(result.aside_count, 1)


class DriverNameTests(ShiftFixtureMixin, TestCase):
    """The 'Unnamed chauffeur' bug: a values() read gave blanks for anyone whose
    profile carries only a username. Use the board's own rule instead."""

    def setUp(self):
        self._vehicle = None
        self.day = date(2026, 9, 20)

    def _leg_for(self, driver):
        vehicle, _, _ = self._base()
        return Leg.objects.create(
            reservation=self.make_reservation(), pickup_date=self.day,
            pickup_time=time(9, 0), pickup_location="MCO",
            dropoff_location="Disney", vehicle=vehicle,
            driver=driver, status="in-progress",
        )

    def test_a_full_name_is_used_when_there_is_one(self):
        user = User.objects.create_user("mensah", first_name="George", last_name="Mensah")
        driver = Driver.objects.create(profile=user, driver_type="inhouse", is_active=True)
        self._leg_for(driver)
        self.assertEqual(
            shift_checks.unconfirmed_chauffeurs(self.day).items[0]["who"],
            "George Mensah",
        )

    def test_a_username_is_used_when_there_is_no_name(self):
        user = User.objects.create_user("sereen")  # no first/last at all
        driver = Driver.objects.create(profile=user, driver_type="inhouse", is_active=True)
        self._leg_for(driver)
        who = shift_checks.unconfirmed_chauffeurs(self.day).items[0]["who"]
        self.assertEqual(who, "sereen")
        self.assertNotIn("Unnamed", who)


class ControlPanelTests(TestCase):
    """The page is a control panel: gate tiles, real checkboxes, one button."""

    def setUp(self):
        self.user = _staff("panel", "Pat")
        self.client.force_login(self.user)
        self.html = self.client.get(reverse("shift_open")).content.decode()

    def test_the_gates_count_down_rather_than_saying_not_yet(self):
        self.assertIn("Board safe by", self.html)
        self.assertIn("Open complete by", self.html)
        self.assertNotIn("not yet", self.html)
        self.assertNotIn("waiting</span>", self.html)

    def test_the_human_rows_are_operable_checkboxes(self):
        """A styled control, but a real one: announced as a checkbox, reachable
        by keyboard, and reporting its state."""
        self.assertIn('role="checkbox"', self.html)
        self.assertIn('data-row=', self.html)
        self.assertIn('aria-checked=', self.html)
        self.assertIn('tabindex="0"', self.html)

    def test_the_first_two_rows_name_the_apps_the_team_actually_uses(self):
        """RingCentral for the phone, WhatsApp for the opening post.

        Both rows are on a person's word, so the label IS the instruction. When
        the phone row said "phone app" and the triage row said "texts", an
        opener had to already know which of the two apps each meant — and the
        team's channel is WhatsApp while the phone is RingCentral, so the old
        wording pointed at the wrong one.
        """
        self.assertIn("RingCentral logged in", self.html)
        self.assertIn("WhatsApp", self.html)
        self.assertNotIn("Phone app", self.html)

    def test_the_phone_row_comes_first_and_names_the_ringer(self):
        checklist = ShiftChecklist.objects.get(kind=ShiftChecklist.Kind.OPEN)
        keys = [r.key for r in checklist.rows.filter(kind="human").order_by("position")]
        self.assertEqual(keys[0], "phone")
        self.assertIn("ringer on", self.html)

    def test_missed_calls_is_gone_as_a_separate_row(self):
        # It was a second RingCentral line and read as a duplicate.
        checklist = ShiftChecklist.objects.get(kind=ShiftChecklist.Kind.OPEN)
        self.assertNotIn("missed_calls", [r.key for r in checklist.rows.all()])

    def test_the_footer_shows_a_tally_and_one_button(self):
        self.assertIn("done", self.html)
        self.assertIn("Complete open", self.html)
        self.assertEqual(self.html.count('data-shift-action="complete"'), 1)

    def test_the_note_button_is_called_note(self):
        self.assertIn(">Note<", self.html)
        self.assertNotIn("Can&#x27;t clear it", self.html)
        self.assertNotIn("Write it up", self.html)


class AcknowledgeAllTests(ShiftFixtureMixin, TestCase):
    """One action clears every outstanding pickup-change badge for the date."""

    def setUp(self):
        self._vehicle = None
        self.user = _staff("acker", "Ack")
        self.day = timezone.localdate()

    def test_acknowledging_all_clears_every_badge(self):
        vehicle, _, _ = self._base()
        reservation = self.make_reservation()
        legs = [
            Leg.objects.create(
                reservation=reservation, pickup_date=self.day,
                pickup_time=time(23, h), pickup_location="MCO",
                dropoff_location="Disney", vehicle=vehicle,
                pickup_time_changed_at=timezone.now(),
            ) for h in (10, 20)
        ]
        self.assertEqual(
            shift_checks.unreviewed_flight_alerts(self.day).aside_count, 2,
        )

        self.client.force_login(self.user)
        self.client.get(reverse("shift_open"))
        checklist = ShiftChecklist.objects.get(
            date=self.day, kind=ShiftChecklist.Kind.OPEN,
        )
        response = self.client.post(
            reverse("shift_action"),
            data={"action": "acknowledge_pickup_changes",
                  "checklist_id": checklist.id},
            content_type="application/json",
        )
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["acked"], 2)
        self.assertEqual(
            shift_checks.unreviewed_flight_alerts(self.day).aside_count, 0,
        )
        for leg in legs:
            leg.refresh_from_db()
            self.assertIsNotNone(leg.pickup_change_ack_at)


class RowReconcileTests(TestCase):
    """An unfinished checklist follows the current settings.

    Without this, changing which rows exist only ever reached TOMORROW's
    checklist, and today's kept rendering a retired row — which is how a raw
    "missed_calls" key ended up in front of the floor.
    """

    def setUp(self):
        self.user = _staff("recon", "Rae")
        self.day = date(2026, 9, 20)
        self.checklist = shift_services.get_or_open_checklist(
            self.day, ShiftChecklist.Kind.OPEN, user=self.user,
        )

    def _keys(self):
        return [r.key for r in self.checklist.rows.order_by("position")]

    def test_a_retired_row_is_dropped_when_nobody_touched_it(self):
        ShiftChecklistRow.objects.create(
            checklist=self.checklist, key="missed_calls",
            kind=ShiftChecklistRow.Kind.HUMAN, position=99,
        )
        self.assertIn("missed_calls", self._keys())
        shift_services.reconcile_rows(self.checklist)
        self.assertNotIn("missed_calls", self._keys())

    def test_a_retired_row_someone_confirmed_is_kept_as_history(self):
        row = ShiftChecklistRow.objects.create(
            checklist=self.checklist, key="missed_calls",
            kind=ShiftChecklistRow.Kind.HUMAN, position=99,
            state=ShiftChecklistRow.State.CLEAR,
            confirmed_by=self.user, confirmed_at=timezone.now(),
        )
        shift_services.reconcile_rows(self.checklist)
        self.assertIn("missed_calls", self._keys())
        row.refresh_from_db()
        self.assertEqual(row.confirmed_by, self.user)

    def test_a_retired_row_with_a_note_against_it_is_kept(self):
        row = ShiftChecklistRow.objects.create(
            checklist=self.checklist, key="missed_calls",
            kind=ShiftChecklistRow.Kind.HUMAN, position=99,
        )
        shift_services.document_exception(
            self.checklist, what="Two callbacks left.", owner=self.user,
            next_action="Do them at 9.", user=self.user, row_key="missed_calls",
        )
        shift_services.reconcile_rows(self.checklist)
        self.assertIn("missed_calls", self._keys())

    def test_a_retired_key_still_renders_a_human_label(self):
        row = ShiftChecklistRow.objects.create(
            checklist=self.checklist, key="missed_calls",
            kind=ShiftChecklistRow.Kind.HUMAN, position=99,
        )
        self.assertEqual(row.label, "Missed calls returned")

    def test_an_unknown_key_never_renders_raw(self):
        row = ShiftChecklistRow.objects.create(
            checklist=self.checklist, key="some_old_thing",
            kind=ShiftChecklistRow.Kind.HUMAN, position=98,
        )
        self.assertEqual(row.label, "Some old thing")
        self.assertNotIn("_", row.label)

    def test_the_phone_row_is_reordered_to_the_front(self):
        # Simulate a checklist created under the old ordering.
        for i, key in enumerate(["sms", "email", "ghl", "phone"]):
            self.checklist.rows.filter(key=key).update(position=100 + i)
        shift_services.reconcile_rows(self.checklist)
        human = [r.key for r in
                 self.checklist.rows.filter(kind="human").order_by("position")]
        self.assertEqual(human[0], "phone")

    def test_a_finished_checklist_is_left_exactly_as_it_was(self):
        ShiftChecklistRow.objects.create(
            checklist=self.checklist, key="missed_calls",
            kind=ShiftChecklistRow.Kind.HUMAN, position=99,
        )
        for row in self.checklist.rows.filter(kind="human"):
            shift_services.confirm_human_row(self.checklist, row.key, self.user)
        shift_services.complete_checklist(self.checklist, self.user)
        before = self._keys()
        shift_services.reconcile_rows(self.checklist)
        self.assertEqual(self._keys(), before)


class FullBleedTests(TestCase):
    """The ground is the page, not the column."""

    def setUp(self):
        self.user = _staff("bleed", "Bee")
        self.client.force_login(self.user)
        self.html = self.client.get(reverse("shift_open")).content.decode()

    def test_the_page_paints_its_own_ground(self):
        self.assertIn("body { background:#f6f4ef; }", self.html)

    def test_the_past_pickup_line_is_not_printed_twice(self):
        # It belongs in the row's own meta, once.
        self.assertLessEqual(self.html.count("already past pickup"), 1)


class ConfirmDialogTests(TestCase):
    """Confirmations happen in the page, not in the browser's own dialog."""

    def setUp(self):
        self.user = _staff("dlg", "Dee")
        self.client.force_login(self.user)
        self.html = self.client.get(reverse("shift_open")).content.decode()

    def test_the_page_carries_its_own_dialog(self):
        self.assertIn('id="shAsk"', self.html)
        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="true"', self.html)

    def test_the_native_confirm_is_only_a_fallback(self):
        # One guarded use, for the case where the dialog element is missing.
        self.assertEqual(self.html.count("confirm("), 1)
        self.assertIn("window.confirm", self.html)

    def test_nothing_uses_a_browser_alert(self):
        self.assertNotIn("alert(", self.html)

    def test_a_tick_carries_its_question(self):
        self.assertIn("data-ask-title=", self.html)
        self.assertIn("data-ask-body=", self.html)
        # Literal template text is not escaped — and an apostrophe is legal
        # inside a double-quoted attribute — so assert the plain words.
        self.assertIn("You've actually checked?", self.html)

    def test_completing_explains_what_it_records(self):
        self.assertIn("recorded under your name", self.html)

    def test_the_dialog_can_be_escaped_and_traps_focus(self):
        self.assertIn("'Escape'", self.html)
        self.assertIn("'Tab'", self.html)


class BootstrapCollisionTests(TestCase):
    """Bootstrap owns .row, .card and .btn.

    `.row > *` forces 12px of inline padding onto every direct child, which
    crushed the 30px status disc to a 6px content box and collapsed the
    checkbox — the page looked nothing like the design for no visible reason.
    """

    def setUp(self):
        self.user = _staff("collide", "Col")
        self.client.force_login(self.user)
        self.html = self.client.get(reverse("shift_open")).content.decode()

    def test_the_panel_uses_no_bootstrap_owned_class_names(self):
        import re

        panel = self.html[self.html.index('id="shiftPanel"'):]
        panel = panel[:panel.index("</script>")]
        owned = {"row", "card", "btn", "container", "col"}
        for match in re.finditer(r'class="([^"]*)"', panel):
            clash = owned & set(match.group(1).split())
            self.assertFalse(clash, f"collides with Bootstrap: {clash} in {match.group(1)!r}")

    def test_the_status_disc_and_checkbox_keep_their_sizes(self):
        self.assertIn(".sh .st{width:30px;height:30px", self.html)
        self.assertIn(".sh .chk{width:22px;height:22px", self.html)


class CountdownTests(ShiftFixtureMixin, TestCase):
    """How long you have is the useful framing, not what time it is."""

    def setUp(self):
        self._vehicle = None
        self.today = timezone.localdate()

    def test_the_wording_scales_with_the_distance(self):
        cases = [
            (0, "now"),
            (30, "in 30 min"),
            (90, "in 1h 30m"),
            (120, "in 2h"),
        ]
        now = timezone.now()
        for minutes, expected in cases:
            dt = timezone.localtime(now + timedelta(minutes=minutes))
            text, _ = shift_checks._countdown(dt.date(), dt.time(), now)
            self.assertEqual(text, expected, f"{minutes} min out")

    def test_a_pickup_inside_the_urgent_window_is_flagged(self):
        now = timezone.now()
        soon = timezone.localtime(now + timedelta(minutes=20))
        later = timezone.localtime(now + timedelta(minutes=300))
        self.assertTrue(shift_checks._countdown(soon.date(), soon.time(), now)[1])
        self.assertFalse(shift_checks._countdown(later.date(), later.time(), now)[1])

    def test_an_unassigned_trip_says_how_long_is_left(self):
        vehicle, _, _ = self._base()
        soon = timezone.localtime(timezone.now() + timedelta(minutes=50))
        if soon.date() != self.today:
            self.skipTest("run crosses midnight")
        Leg.objects.create(
            reservation=self.make_reservation(), pickup_date=self.today,
            pickup_time=soon.time(), pickup_location="MCO",
            dropoff_location="Disney", vehicle=vehicle,
        )
        item = shift_checks.unassigned_legs(self.today).items[0]
        self.assertTrue(item["until"].startswith("in "))
        self.assertIn("min", item["until"])

    def test_an_unconfirmed_chauffeur_says_when_their_first_job_is(self):
        vehicle, _, _ = self._base()
        soon = timezone.localtime(timezone.now() + timedelta(minutes=40))
        if soon.date() != self.today:
            self.skipTest("run crosses midnight")
        driver = self.make_driver("cd1", "Cass")
        Leg.objects.create(
            reservation=self.make_reservation(), pickup_date=self.today,
            pickup_time=soon.time(), pickup_location="MCO",
            dropoff_location="Disney", vehicle=vehicle,
            driver=driver, status="in-progress",
        )
        item = shift_checks.unconfirmed_chauffeurs(self.today).items[0]
        self.assertTrue(item["until"].startswith("in "))
        self.assertTrue(item["urgent"], "40 minutes out is worth shouting about")

    def test_the_page_renders_the_countdown(self):
        vehicle, _, _ = self._base()
        soon = timezone.localtime(timezone.now() + timedelta(minutes=55))
        if soon.date() != self.today:
            self.skipTest("run crosses midnight")
        Leg.objects.create(
            reservation=self.make_reservation(), pickup_date=self.today,
            pickup_time=soon.time(), pickup_location="MCO",
            dropoff_location="Disney", vehicle=vehicle,
        )
        user = _staff("cdview", "Cee")
        self.client.force_login(user)
        html = self.client.get(reverse("shift_open")).content.decode()
        # Not pinned to the exact minute — a second of drift between building
        # the fixture and rendering would make that flaky for no reason.
        self.assertRegex(html, r"in 5\d min")


class RowClickTests(TestCase):
    """A 22px box is a small target — the whole row takes the click."""

    def setUp(self):
        self.user = _staff("rowclick", "Ro")
        self.client.force_login(self.user)
        self.html = self.client.get(reverse("shift_open")).content.decode()

    def test_the_row_is_clickable(self):
        self.assertIn(".sh .shr.human,.sh .shr.carry{cursor:pointer}", self.html)
        self.assertIn("box.click();", self.html)

    def test_controls_inside_the_row_keep_their_own_click(self):
        self.assertIn(
            "e.target.closest('button, a, input, select, textarea, .chk, .note')",
            self.html,
        )


class ConfirmDialogScopeTests(TestCase):
    """The dialog reads the panel's palette, so it has to live inside it.

    It once sat after the panel closed: every `.sh …` rule missed it, the
    background resolved to nothing and the confirm rendered see-through with
    raw browser buttons on top of the page.
    """

    def setUp(self):
        self.client.force_login(_staff("dlgscope", "Dl"))
        self.html = self.client.get(reverse("shift_open")).content.decode()

    def test_the_dialog_sits_inside_the_panel(self):
        from html.parser import HTMLParser

        class Finder(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.panel_depth = None
                self.inside = False

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag != "div":
                    return
                self.stack.append(a)
                if a.get("id") == "shiftPanel":
                    self.panel_depth = len(self.stack)
                if a.get("id") == "shAsk":
                    self.inside = (
                        self.panel_depth is not None
                        and len(self.stack) > self.panel_depth
                    )

            def handle_endtag(self, tag):
                if tag == "div" and self.stack:
                    if len(self.stack) == self.panel_depth:
                        self.panel_depth = None
                    self.stack.pop()

        f = Finder()
        f.feed(self.html)
        self.assertTrue(f.inside, "the confirm dialog escaped #shiftPanel")

    def test_the_dialog_paints_its_own_ground(self):
        # Not var(--card): a token that fails to resolve is invisible, and an
        # invisible dialog is worse than an ugly one.
        self.assertIn(".sh-dlg{background:#fff;", self.html)


class GateAllowanceTests(ShiftFixtureMixin, TestCase):
    """The SOP measures the open in elapsed minutes; the wall clock is the floor.

    "BOARD SAFE ≤ 45 MIN" is the opener's work rate. 7:15 is what the first
    pickup needs. Both are real, so whichever lands first is the gate.
    """

    def setUp(self):
        self._vehicle = None
        self.user = _staff("gale", "Gale")
        self.today = timezone.localdate()
        self.checklist = shift_services.get_or_open_checklist(
            self.today, ShiftChecklist.Kind.OPEN, user=self.user,
        )

    def _opened_at(self, hh, mm):
        stamp = timezone.make_aware(
            timezone.datetime.combine(self.today, time(hh, mm)),
            timezone.get_current_timezone(),
        )
        self.checklist.opened_at = stamp
        return stamp

    def test_an_early_start_is_held_to_the_allowance(self):
        # Sat down at 5:00, so the board is due safe at 5:45 — not 7:15, which
        # would hand a two-hour head start to whoever came in early.
        self._opened_at(5, 0)
        target = self.checklist._target_dt("board_safe")
        self.assertEqual(timezone.localtime(target).strftime("%H:%M"), "05:45")
        self.assertEqual(self.checklist.target_basis("board_safe"), "allowance")

    def test_a_late_start_is_held_to_the_wall_clock(self):
        # Sat down at 7:00. The allowance would say 7:45, but the first pickup
        # does not care what time the opener arrived.
        self._opened_at(7, 0)
        target = self.checklist._target_dt("board_safe")
        self.assertEqual(timezone.localtime(target).strftime("%H:%M"), "07:15")
        self.assertEqual(self.checklist.target_basis("board_safe"), "clock")

    def test_the_full_open_carries_its_own_allowance(self):
        self._opened_at(5, 0)
        target = self.checklist._target_dt("open_complete")
        self.assertEqual(timezone.localtime(target).strftime("%H:%M"), "06:30")

    def test_opening_ahead_of_the_day_falls_back_to_the_wall_clock(self):
        # A checklist opened for a date that is not today has no meaningful
        # "minutes since you sat down".
        future = shift_services.get_or_open_checklist(
            self.today + timedelta(days=8), ShiftChecklist.Kind.OPEN, user=self.user,
        )
        self.assertIsNone(future._allowance_start())
        self.assertEqual(future.target_basis("board_safe"), "clock")
        target = future._target_dt("board_safe")
        self.assertEqual(timezone.localtime(target).strftime("%H:%M"), "07:15")

    def test_the_tile_says_which_limit_is_binding(self):
        self._opened_at(5, 0)
        self.checklist.save(update_fields=["opened_at"])
        self.client.force_login(self.user)
        html = self.client.get(reverse("shift_open")).content.decode()
        self.assertIn("45 min in", html)


class ShiftMenuTests(TestCase):
    """You are offered the checklist you are down to work, and not the other.

    Two links that both look equally like yours is how a dispatcher fills in
    the wrong one — which is what was happening, and why a close checklist was
    being worked as though it were an open.
    """

    def setUp(self):
        from django.core.cache import cache
        from ops.models import StaffWeeklySchedule

        cache.clear()  # the day's duties are cached for the whole floor
        self.today = timezone.localdate()
        dow = self.today.weekday()

        self.opener = _staff("menu_opener", "Abdalla")
        self.closer = _staff("menu_closer", "Priya")
        self.midday = _staff("menu_midday", "Iris")

        # Windows span the day so the suite does not pass or fail on the hour
        # it happens to run; the ROLES are what these tests are about.
        for user, role in ((self.opener, "opener"), (self.closer, "closer"),
                           (self.midday, "")):
            StaffWeeklySchedule.objects.create(
                user=user, day_of_week=dow, is_working=True,
                start_time=time(0, 30), end_time=time(23, 59), role=role,
            )

    def _menu(self, user):
        self.client.force_login(user)
        html = self.client.get(reverse("dashboard")).content.decode()
        return {
            "open": "Open Shift</a>" in html,
            "close": "Close Shift</a>" in html,
        }

    def test_the_opener_is_offered_the_open_and_not_the_close(self):
        self.assertEqual(self._menu(self.opener), {"open": True, "close": False})

    def test_the_closer_is_offered_the_close_and_not_the_open(self):
        self.assertEqual(self._menu(self.closer), {"open": False, "close": True})

    def test_someone_with_no_duty_still_sees_both(self):
        """A mid-day dispatcher has no shift of their own to be steered to.

        They are also the person most likely to be covering, so there is no
        basis to hide either checklist from them.
        """
        self.assertEqual(self._menu(self.midday), {"open": True, "close": True})

    def test_the_lead_sees_both_ends_of_the_day(self):
        from django.contrib.auth.models import Permission
        lead = _staff("menu_lead", "Sam")
        lead.user_permissions.add(
            Permission.objects.get(codename="review_checklists"))
        # Also down to open — the lead's oversight still wins.
        from ops.models import StaffWeeklySchedule
        StaffWeeklySchedule.objects.create(
            user=lead, day_of_week=self.today.weekday(), is_working=True,
            start_time=time(0, 1), end_time=time(23, 59), role="opener",
        )
        self.assertEqual(self._menu(lead), {"open": True, "close": True})

    def test_an_empty_roster_never_costs_anyone_their_checklists(self):
        """A schedule nobody has filled in must not hide the work."""
        from django.core.cache import cache
        from ops.models import StaffWeeklySchedule

        StaffWeeklySchedule.objects.all().delete()
        cache.clear()
        self.assertEqual(self._menu(self.midday), {"open": True, "close": True})

    def test_the_pages_stay_reachable_for_whoever_is_covering(self):
        """The MENU is narrowed; the doors are not locked.

        Someone covering an unscheduled close at 9 PM needs a way in that does
        not involve editing the roster first — a tidy menu is not worth a shift
        that cannot be closed.
        """
        self.client.force_login(self.opener)
        self.assertEqual(
            self.client.get(reverse("shift_close")).status_code, 200)
