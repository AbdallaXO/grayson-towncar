"""The eye-scan, done by the machine.

Guards the one thing this module exists for: after a flight match moves a
pickup, the opener should not have to read the board line by line to find the
turn it broke.
"""

from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

from ops import move_impact
from dispatching.pickup_moves import apply_pickup_time_move


class MoveImpactMixin:
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

    def make_driver(self, username, first):
        user = User.objects.create_user(
            username=username, password="x", first_name=first, last_name="Chase",
        )
        return Driver.objects.create(profile=user, driver_type="inhouse", is_active=True)

    def leg_at(self, when, driver=None, **kw):
        vehicle, _, _ = self._base()
        defaults = dict(
            reservation=self.reservation, pickup_date=self.day,
            pickup_location="Disney", dropoff_location="MCO",
            vehicle=vehicle, pickup_time=when, driver=driver,
            status="confirmed",
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class ClashDetectionTests(MoveImpactMixin, TestCase):
    """The 20 minutes that caused the crash."""

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate() + timedelta(days=1)
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("mel", "Mel")

    def test_a_quiet_board_reports_nothing(self):
        self.leg_at(time(8, 0), self.driver)
        self.leg_at(time(14, 0), self.driver)
        self.assertEqual(move_impact.clashes_from_moves(self.day), [])

    def test_a_move_that_breaks_a_turn_is_found_without_scanning(self):
        # Two jobs, six hours apart — comfortable.
        first = self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        self.assertEqual(move_impact.clashes_from_moves(self.day), [])

        # The flight match pulls the second one back on top of the first.
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        found = move_impact.clashes_from_moves(self.day)
        self.assertEqual(len(found), 1, "the broken turn was not reported")
        clash = found[0]
        self.assertEqual(clash.prev_leg.pk, first.pk)
        self.assertEqual(clash.curr_leg.pk, second.pk)
        self.assertEqual(clash.driver_name, "Mel Chase")
        self.assertGreater(clash.late_now, 0)

    def test_it_says_the_turn_used_to_work(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        clash = move_impact.clashes_from_moves(self.day)[0]
        self.assertTrue(clash.is_new, "a turn that worked before reads as new")
        self.assertEqual(clash.verdict, "This turn worked before that.")
        self.assertIn("earlier", clash.why)
        self.assertIn("originally 2:00 PM", clash.why, "the before time is spelled out")

    def test_the_two_jobs_are_not_written_as_a_time_change(self):
        # An arrow between two clock times reads as "this moved from A to B",
        # which contradicts the move line underneath it.
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        clash = move_impact.clashes_from_moves(self.day)[0]
        self.assertEqual(clash.pair_label, "8:00 AM job into the 8:20 AM")
        self.assertNotIn("\u2192", clash.pair_label)

    def test_each_moved_pickup_gets_its_own_line(self):
        first = self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")
        apply_pickup_time_move(first, time(7, 50), note="Flight match")

        clash = move_impact.clashes_from_moves(self.day)[0]
        self.assertEqual(len(clash.move_notes), 2)
        for note in clash.move_notes:
            self.assertIn("originally ", note, "every move says where it came from")

    def test_the_move_is_described_in_plain_words(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(13, 40), note="Flight match")
        moves = move_impact.recent_moves(self.day)
        self.assertEqual(moves[second.pk].phrase, "20 min earlier")

        third = self.leg_at(time(17, 0), self.driver)
        apply_pickup_time_move(third, time(18, 30), note="Flight match")
        self.assertEqual(move_impact.recent_moves(self.day)[third.pk].phrase,
                         "1h 30m later")

    def test_a_standing_conflict_nobody_moved_is_not_reported(self):
        # Both jobs were always on top of each other. That is the board's
        # ordinary red badge, not something this refresh did.
        self.leg_at(time(8, 0), self.driver)
        self.leg_at(time(8, 20), self.driver)
        self.assertEqual(move_impact.clashes_from_moves(self.day), [])

    def test_acknowledging_clears_the_ghost_but_not_the_break(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")
        self.assertEqual(len(move_impact.clashes_from_moves(self.day)), 1)
        self.assertIn(second.pk, move_impact.recent_moves(self.day))

        # Ticking the change says "I have seen that this moved". It does not
        # say "I have re-seated the chauffeur", so the broken turn stays.
        Leg.objects.filter(pk=second.pk).update(pickup_change_ack_at=timezone.now())
        self.assertEqual(move_impact.recent_moves(self.day), {})
        self.assertEqual(len(move_impact.clashes_from_moves(self.day)), 1)

    def test_a_break_clears_when_the_turn_is_actually_fixed(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")
        self.assertEqual(len(move_impact.clashes_from_moves(self.day)), 1)

        # Somebody moves it back out of the way. Nothing is broken any more.
        apply_pickup_time_move(second, time(15, 0), note="Reseated")
        self.assertEqual(move_impact.clashes_from_moves(self.day), [])

    def test_another_chauffeurs_chain_is_not_dragged_in(self):
        other = self.make_driver("sam", "Sam")
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        self.leg_at(time(8, 5), other)
        self.leg_at(time(15, 0), other)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        found = move_impact.clashes_from_moves(self.day)
        self.assertEqual({c.driver_id for c in found}, {self.driver.pk})


class ReadOnlyTests(MoveImpactMixin, TestCase):
    """It reads the board. It must never write to it."""

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate() + timedelta(days=1)
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("ida", "Ida")

    def test_the_rewind_never_reaches_the_database(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        before = Leg.objects.get(pk=second.pk)
        stamped = before.pickup_time_changed_at
        move_impact.clashes_from_moves(self.day)
        after = Leg.objects.get(pk=second.pk)

        # The rewind runs on copies. If it ever touched the real row it would
        # put the pickup back AND wipe the badge that says it moved.
        self.assertEqual(after.pickup_time, time(8, 20))
        self.assertEqual(after.pickup_time_was, time(14, 0))
        self.assertEqual(after.pickup_time_changed_at, stamped)
        self.assertTrue(after.has_unacked_time_change)

    def test_it_files_no_tasks(self):
        from ops.models import OperationalTask

        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")
        move_impact.clashes_from_moves(self.day)
        self.assertEqual(OperationalTask.objects.count(), 0)


class WindowTests(MoveImpactMixin, TestCase):
    """A move ages out whether or not anyone clicks the badge.

    Acknowledging a pickup change happens on the legs dashboard; the people who
    run the schedule work on the drag-and-drop board. Gating only on the flag
    would leave Tuesday's move still shouting on Friday.
    """

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate() + timedelta(days=1)
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("win", "Win")
        self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")

    def test_a_fresh_move_is_in(self):
        self.assertEqual(len(move_impact.clashes_from_moves(self.day)), 1)

    def test_a_stale_move_ages_out_even_unacknowledged(self):
        old = timezone.now() - timedelta(hours=move_impact.MOVE_WINDOW_HOURS + 1)
        Leg.objects.filter(pk=self.second.pk).update(pickup_time_changed_at=old)
        still_unacked = Leg.objects.get(pk=self.second.pk)
        self.assertTrue(still_unacked.has_unacked_time_change)
        self.assertEqual(move_impact.clashes_from_moves(self.day), [])

    def test_the_window_edge_is_inclusive_enough_for_an_overnight(self):
        # Moved at last night's close, read at this morning's open.
        old = timezone.now() - timedelta(hours=move_impact.MOVE_WINDOW_HOURS - 1)
        Leg.objects.filter(pk=self.second.pk).update(pickup_time_changed_at=old)
        self.assertEqual(len(move_impact.clashes_from_moves(self.day)), 1)


class LinkTests(MoveImpactMixin, TestCase):
    """The big schedule is the board. 180 jobs do not fit the legs dashboard."""

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()

    def test_board_shaped_rows_point_at_the_board(self):
        from ops import shift_checks

        for key in ("unassigned", "unconfirmed", "moves"):
            result = shift_checks.CHECKS[key](self.day)
            self.assertIn("/schedule-board/", result.url, key)
            self.assertEqual(result.url_label, "Board", key)

    def test_task_rows_say_they_go_to_the_tasks(self):
        from ops import shift_checks

        for key in ("flight", "conflicts"):
            result = shift_checks.CHECKS[key](self.day)
            self.assertEqual(result.url_label, "Tasks", key)


class BoardBannerTests(MoveImpactMixin, TestCase):
    """The board is where the schedule is actually read, so it says so there.

    The checklist answers this once, at 6:45. Matches happen all day.
    """

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("bo", "Bo")
        self.staff = User.objects.create_user(
            username="boardie", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)

    def _board(self, **params):
        params.setdefault("date", self.day.isoformat())
        return self.client.get("/dispatching/schedule-board/", params)

    def test_a_quiet_board_shows_no_banner(self):
        self.leg_at(time(8, 0), self.driver)
        self.leg_at(time(14, 0), self.driver)
        html = self._board().content.decode()
        self.assertNotIn('id="movesBanner"', html)

    def test_a_broken_turn_is_called_out_above_the_timeline(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        html = self._board().content.decode()
        self.assertIn('id="movesBanner"', html)
        self.assertIn("1 turn broken", html)
        self.assertIn("min short", html)

    def test_the_affiliate_board_stays_out_of_it(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        html = self._board(view="affiliate").content.decode()
        self.assertNotIn('id="movesBanner"', html)

    def test_a_broken_detector_never_costs_anyone_the_board(self):
        # A banner that fails to draw is a nuisance. A board that fails to load
        # is a day.
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")

        from unittest.mock import patch

        with patch("ops.move_impact.clashes_from_moves",
                   side_effect=RuntimeError("detector exploded")):
            with self.assertLogs("dispatching.views", level="ERROR"):
                response = self._board()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="movesBanner"', response.content.decode())


class BannerWordingTests(MoveImpactMixin, TestCase):
    """The banner is one line: how many, when, and the controls.

    It deliberately no longer restates each break. That detail is drawn ON the
    board now — row markers, ghosts, brackets — and a banner repeating it would
    be a second place to read the same thing, with two versions to keep true.
    """

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("cw", "Cass")
        self.staff = User.objects.create_user(
            username="reader", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(8, 20), note="Flight match")
        self.html = self.client.get(
            "/dispatching/schedule-board/", {"date": self.day.isoformat()},
        ).content.decode()

    def test_it_says_the_count_and_when(self):
        self.assertIn("1 turn broken", self.html)
        self.assertIn("pickup moved", self.html)
        self.assertIn("last refresh", self.html)

    def test_it_carries_the_controls_and_nothing_else(self):
        self.assertIn('id="movesPrev"', self.html)
        self.assertIn('id="movesNext"', self.html)
        self.assertNotIn('id="movesOnly"', self.html)
        self.assertNotIn("job into the", self.html)

    def test_am_pm_survives_in_the_clash_wording(self):
        # A .capitalize() on the whole sentence once turned "8:20 AM" into
        # "8:20 am".
        clash = move_impact.clashes_from_moves(self.day)[0]
        self.assertIn("2:00 PM", clash.why)
        self.assertNotIn(" pm", clash.why)


class ChangeSetTests(MoveImpactMixin, TestCase):
    """One object, computed once per refresh, that the board can draw from."""

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("cs", "Cy")
        self.first = self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)

    def test_a_quiet_day_is_empty(self):
        changes = move_impact.change_set(self.day, use_cache=False)
        self.assertTrue(changes.is_empty)
        self.assertEqual(changes.rows, {})

    def test_it_marks_the_row_the_leg_and_the_break(self):
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")
        changes = move_impact.change_set(self.day, use_cache=False)

        self.assertEqual(changes.break_count, 1)
        self.assertEqual(changes.moved_count, 1)
        self.assertEqual(changes.row(self.driver.pk)["breaks"], 1)
        self.assertTrue(changes.row(self.driver.pk)["moved"])
        # Both legs in the overlap are marked — neither is context for the other.
        self.assertTrue(changes.leg(self.first.pk)["in_break"])
        self.assertTrue(changes.leg(self.second.pk)["in_break"])
        self.assertIsNone(changes.leg(self.first.pk)["move"])
        self.assertIsNotNone(changes.leg(self.second.pk)["move"])

    def test_a_move_that_breaks_nothing_still_marks_the_row(self):
        apply_pickup_time_move(self.second, time(13, 40), note="Flight match")
        changes = move_impact.change_set(self.day, use_cache=False)
        self.assertEqual(changes.break_count, 0)
        self.assertTrue(changes.row(self.driver.pk)["moved"])

    def test_acknowledging_drops_the_ghost_and_keeps_the_row_marked(self):
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")
        Leg.objects.filter(pk=self.second.pk).update(
            pickup_change_ack_at=timezone.now(),
        )
        changes = move_impact.change_set(self.day, use_cache=False)
        self.assertEqual(changes.moved_count, 0, "ghost goes")
        self.assertEqual(changes.break_count, 1, "bracket stays")
        self.assertFalse(changes.row(self.driver.pk)["moved"])
        self.assertEqual(changes.row(self.driver.pk)["breaks"], 1)


class ChangeSetCacheTests(MoveImpactMixin, TestCase):
    """Once per refresh, not once per render."""

    def setUp(self):
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("cc", "Cache")
        self.first = self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)
        from django.core.cache import cache
        cache.clear()

    def test_a_second_render_does_not_recompute(self):
        from unittest.mock import patch

        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")
        move_impact.change_set(self.day)
        with patch("ops.move_impact._build_change_set") as build:
            again = move_impact.change_set(self.day)
        build.assert_not_called()
        self.assertEqual(again.break_count, 1)

    def test_a_new_move_invalidates_it_by_itself(self):
        apply_pickup_time_move(self.second, time(13, 40), note="Flight match")
        first_pass = move_impact.change_set(self.day)
        self.assertEqual(first_pass.break_count, 0)

        # No cache clearing anywhere — the key carries the moves' own stamp.
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")
        self.assertEqual(move_impact.change_set(self.day).break_count, 1)

    def test_acknowledging_invalidates_it_too(self):
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")
        self.assertEqual(move_impact.change_set(self.day).moved_count, 1)
        Leg.objects.filter(pk=self.second.pk).update(
            pickup_change_ack_at=timezone.now(),
        )
        self.assertEqual(move_impact.change_set(self.day).moved_count, 0)
