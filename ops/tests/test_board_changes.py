"""Changes mode on the schedule board.

A dispatcher should be able to find every row a flight refresh touched from the
driver column alone, see where each moved leg came from, and step through the
broken turns without reading the board line by line.
"""

import re
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation
from dispatching.pickup_moves import apply_pickup_time_move
from dispatching import board_changes


class BoardMixin:
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
            username=username, password="x", first_name=first, last_name="Reed",
        )
        return Driver.objects.create(
            profile=user, driver_type="inhouse", is_active=True,
        )

    def leg_at(self, when, driver=None, **kw):
        vehicle, _, _ = self._base()
        defaults = dict(
            reservation=self.reservation, pickup_date=self.day,
            pickup_location="Disney", dropoff_location="MCO",
            vehicle=vehicle, pickup_time=when, driver=driver, status="confirmed",
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def board(self, **params):
        params.setdefault("date", self.day.isoformat())
        return self.client.get("/dispatching/schedule-board/", params)

    def markup(self, html=None):
        """Just the drawn rows — no stylesheet, no script.

        The class names appear in the <style> block by definition, so asserting
        one is ABSENT against the whole page always fails.
        """
        html = html if html is not None else self.board().content.decode()
        body = html.split('id="boardRows"', 1)[-1]
        body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
        return re.sub(r"<script.*?</script>", "", body, flags=re.S)


class ChangesModeTests(BoardMixin, TestCase):
    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("rhea", "Rhea")
        self.quiet = self.make_driver("quinn", "Quinn")
        self.staff = User.objects.create_user(
            username="dispatch", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)

        self.first = self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)
        self.leg_at(time(9, 0), self.quiet)

    def _broken_board(self):
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")
        return self.board().content.decode()

    def test_a_quiet_board_offers_no_modes_at_all(self):
        html = self.board().content.decode()
        self.assertNotIn('id="boardModes"', html)
        self.assertNotIn('id="movesBanner"', html)
        self.assertNotIn("chg-row", self.markup(html))

    def test_the_affected_row_is_findable_from_the_driver_column(self):
        html = self._broken_board()
        self.assertIn("chg-row", self.markup(html))
        self.assertIn("chg-row-break", self.markup(html))
        self.assertIn('class="chg-badge chg-badge-break"', html)
        self.assertIn("1 broken turn", html)

    def test_a_move_that_breaks_nothing_still_marks_the_row_as_moved(self):
        apply_pickup_time_move(self.second, time(13, 40), note="Flight match")
        body = self.markup()
        self.assertIn("chg-row", body)
        self.assertNotIn("chg-row-break", body)
        self.assertIn(">moved<", body)

    def test_the_moved_leg_carries_a_signed_delta_and_a_ghost(self):
        body = self.markup(self._broken_board())
        self.assertIn("chg-moved", body)
        self.assertIn("chg-delta", body)
        self.assertIn("−340", body, "signed minutes, minus for earlier")
        self.assertIn("chg-ghost", body)
        self.assertIn("Original time 2:00 PM", body)
        self.assertIn("chg-link", body)

    def test_both_legs_of_a_broken_turn_render_at_full_strength(self):
        body = self.markup(self._broken_board())
        self.assertEqual(body.count("chg-in-break"), 2)

    def test_the_break_gets_a_labelled_bracket(self):
        body = self.markup(self._broken_board())
        self.assertIn("chg-bracket", body)
        self.assertIn("min short", body)
        self.assertIn(f"chg-{self.driver.pk}-{self.first.pk}-{self.second.pk}", body)

    def test_both_modes_are_offered(self):
        html = self._broken_board()
        for mode in ("all", "highlight"):
            self.assertIn(f'data-board-mode="{mode}"', html)

    def test_no_mode_hides_a_row(self):
        """Highlight dims; nothing removes rows.

        A board with rows taken out of it stops being the day — you lose the
        context that says where the moved job could go instead — so the
        recede-don't-hide rule is the point of the feature, not a detail.
        """
        html = self._broken_board()
        self.assertNotIn('data-board-mode="only"', html)
        self.assertNotIn("board-mode-only", html)

    def test_the_banner_is_one_line_with_the_controls(self):
        html = self._broken_board()
        self.assertIn('id="movesBanner"', html)
        self.assertIn("turn broken", html)
        self.assertIn("pickup moved", html)
        self.assertIn("last refresh", html)
        self.assertIn('id="movesPrev"', html)
        self.assertIn('id="movesNext"', html)

    def test_the_quiet_row_is_not_marked(self):
        body = self.markup(self._broken_board())
        self.assertIn(f'data-driver-id="{self.quiet.pk}" data-driver-name="Quinn Reed" '
                      f'data-chg-breaks="0" data-chg-moved="0"', body)


class ClearingTests(BoardMixin, TestCase):
    """Markers stop when the thing they mark stops."""

    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("cleo", "Cleo")
        self.staff = User.objects.create_user(
            username="clr", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)
        self.first = self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")

    def test_acknowledging_clears_the_ghost_and_keeps_the_bracket(self):
        Leg.objects.filter(pk=self.second.pk).update(
            pickup_change_ack_at=timezone.now(),
        )
        body = self.markup()
        self.assertNotIn("chg-ghost", body, "the ghost goes when it is ticked")
        self.assertIn("chg-bracket", body, "the broken turn is still broken")
        self.assertIn("chg-row-break", body)

    def test_fixing_the_turn_clears_everything(self):
        apply_pickup_time_move(self.second, time(15, 30), note="Reseated")
        Leg.objects.filter(pk=self.second.pk).update(
            pickup_change_ack_at=timezone.now(),
        )
        html = self.board().content.decode()
        body = self.markup(html)
        self.assertNotIn("chg-bracket", body)
        self.assertNotIn("chg-row", body)
        self.assertNotIn('id="movesBanner"', html)


class ReadOnlyBoardTests(BoardMixin, TestCase):
    """Changes mode writes nothing."""

    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("ro", "Ro")
        self.staff = User.objects.create_user(
            username="ro2", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)
        self.first = self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")

    def test_rendering_the_board_changes_no_leg(self):
        before = {
            leg.pk: (leg.driver_id, leg.pickup_time, leg.pickup_time_was,
                     leg.pickup_change_ack_at)
            for leg in Leg.objects.filter(pickup_date=self.day)
        }
        self.board()
        self.board(**{"date": self.day.isoformat()})
        after = {
            leg.pk: (leg.driver_id, leg.pickup_time, leg.pickup_time_was,
                     leg.pickup_change_ack_at)
            for leg in Leg.objects.filter(pickup_date=self.day)
        }
        self.assertEqual(before, after)

    def test_the_annotator_is_inert_on_an_empty_change_set(self):
        rows = [{"driver": self.driver, "schedule": None}]
        self.assertEqual(
            board_changes.annotate(
                rows, None, selected_date=self.day,
                day_left_dt=None, total_display_minutes=960,
            ),
            [],
        )
        self.assertNotIn("chg_marked", rows[0])


class BudgetTests(BoardMixin, TestCase):
    """The board is a ~2s page on a busy day. This has to be invisible in it."""

    DRIVERS = 20
    PER_DRIVER = 12          # 240 legs, a real Orlando Saturday

    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.staff = User.objects.create_user(
            username="busy", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)

        self.moved = []
        for d in range(self.DRIVERS):
            driver = self.make_driver(f"busy{d}", f"D{d}")
            hour = 6
            for j in range(self.PER_DRIVER):
                leg = self.leg_at(time(hour % 24, 0), driver)
                hour += 1
                if j == 3 and d < 6:
                    self.moved.append(leg)

        # Six chauffeurs get a pickup dragged back on top of the job before it.
        for leg in self.moved:
            apply_pickup_time_move(
                leg, time((leg.pickup_time.hour - 1) % 24, 10), note="Flight match",
            )

    def test_the_change_set_is_a_bounded_number_of_queries(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from ops.move_impact import change_set

        with CaptureQueriesContext(connection) as ctx:
            changes = change_set(self.day, use_cache=False)
        self.assertGreater(changes.break_count, 0, "the fixture should break turns")
        # One pass over the board plus the turn maths. NOT a query per pair:
        # 240 legs across 20 chauffeurs is 220 consecutive pairs, so anything
        # that scales with pairs would show up here immediately.
        self.assertLess(
            len(ctx.captured_queries), 40,
            f"change set took {len(ctx.captured_queries)} queries — "
            "something is querying per leg or per pair",
        )

    def test_a_cached_change_set_costs_one_query(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from ops.move_impact import change_set

        change_set(self.day)
        with CaptureQueriesContext(connection) as ctx:
            change_set(self.day)
        # Just the fingerprint that proves the cache is still good.
        self.assertLessEqual(len(ctx.captured_queries), 2)

    def test_the_busy_board_still_renders(self):
        response = self.board()
        self.assertEqual(response.status_code, 200)
        body = self.markup(response.content.decode())
        self.assertIn("chg-bracket", body)
        self.assertIn("chg-ghost", body)


class LayoutTests(BoardMixin, TestCase):
    """Nothing may be drawn on top of anything else.

    The first cut floated the bracket label 33px below the pill, which on a
    32px lane pitch put "76 min short" across the next chauffeur's 10 AM.
    """

    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("lay", "Lay")
        self.staff = User.objects.create_user(
            username="lay2", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)

    def _row(self, brackets, lanes=1, bar=34):
        return {
            "driver": self.driver,
            "schedule": None,
            "row_lanes": lanes,
            "row_bar_height": bar,
            "chg_brackets": brackets,
        }

    def test_brackets_sit_below_every_pill_lane(self):
        row = self._row([
            {"left_pct": 10, "width_pct": 5, "anchor": "a"},
        ], lanes=2, bar=66)
        board_changes._pack_brackets(row)
        self.assertGreaterEqual(row["chg_brackets"][0]["top_px"], 66)
        self.assertTrue(row["chg_needs_height"])

    def test_the_row_grows_to_hold_the_band(self):
        row = self._row([{"left_pct": 10, "width_pct": 5, "anchor": "a"}])
        board_changes._pack_brackets(row)
        self.assertGreater(row["row_bar_height"], 34)

    def test_overlapping_brackets_stack_instead_of_printing_over_each_other(self):
        row = self._row([
            {"left_pct": 10, "width_pct": 20, "anchor": "a"},
            {"left_pct": 15, "width_pct": 20, "anchor": "b"},
        ])
        board_changes._pack_brackets(row)
        tops = sorted(b["top_px"] for b in row["chg_brackets"])
        self.assertNotEqual(tops[0], tops[1], "they would be drawn on each other")
        self.assertGreaterEqual(
            tops[1] - tops[0], board_changes.BRACKET_LANE_H,
        )

    def test_brackets_that_clear_each_other_share_a_line(self):
        row = self._row([
            {"left_pct": 5, "width_pct": 8, "anchor": "a"},
            {"left_pct": 60, "width_pct": 8, "anchor": "b"},
        ])
        board_changes._pack_brackets(row)
        tops = {b["top_px"] for b in row["chg_brackets"]}
        self.assertEqual(len(tops), 1, "no need for a second line")

    def test_a_row_with_no_brackets_is_left_alone(self):
        row = self._row([])
        board_changes._pack_brackets(row)
        self.assertEqual(row["row_bar_height"], 34)
        self.assertNotIn("chg_needs_height", row)


class PopupTests(BoardMixin, TestCase):
    """Hovering a moved job says where it came from."""

    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("pop", "Pop")
        self.staff = User.objects.create_user(
            username="pop2", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)
        self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(self.second, time(11, 30), note="Flight match")

    def test_the_moved_leg_carries_its_old_time_for_the_popup(self):
        body = self.markup()
        self.assertIn('data-chg-was="2:00 PM"', body)
        self.assertIn('data-chg-delta="−150"', body)

    def test_an_unmoved_leg_carries_nothing(self):
        body = self.markup()
        self.assertIn('data-chg-was=""', body)

    def test_the_popup_builds_a_was_row(self):
        html = self.board().content.decode()
        self.assertIn("el.dataset.chgWas", html)

    def test_the_ghost_carries_no_text_of_its_own(self):
        # Its time is in the tooltip and the popup. Printed on the board it is
        # a second layer of numbers over a 240-leg day.
        body = self.markup()
        self.assertIn('title="Original time 2:00 PM"', body)
        self.assertNotIn("<span>2:00 PM</span>", body)


class NoiseFloorTests(BoardMixin, TestCase):
    """A four-minute reshuffle is weather, not news.

    AeroAPI re-times flights on nearly every refresh, which on one real day put
    46 "moved" pickups on the board. Only moves worth acting on are drawn —
    unless a small one actually broke a turn.
    """

    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("noisy", "Noel")
        self.staff = User.objects.create_user(
            username="noise", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_a_small_shuffle_is_not_reported(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(13, 56), note="Flight match")

        from ops.move_impact import change_set
        changes = change_set(self.day, use_cache=False)
        self.assertEqual(changes.moved_count, 0)
        self.assertTrue(changes.is_empty)
        self.assertNotIn("chg-row", self.markup())

    def test_a_move_at_the_threshold_is_reported(self):
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(second, time(13, 50), note="Flight match")

        from ops.move_impact import change_set
        self.assertEqual(change_set(self.day, use_cache=False).moved_count, 1)

    def test_a_small_move_that_breaks_a_turn_is_still_drawn(self):
        # Size filters noise, never consequence.
        self.leg_at(time(8, 0), self.driver)
        second = self.leg_at(time(8, 25), self.driver)
        apply_pickup_time_move(second, time(8, 19), note="Flight match")

        from ops.move_impact import change_set
        changes = change_set(self.day, use_cache=False)
        self.assertEqual(changes.break_count, 1)
        self.assertEqual(changes.moved_count, 1, "its ghost still shows")
        body = self.markup()
        self.assertIn("chg-bracket", body)
        self.assertIn("chg-ghost", body)


class DismissTests(BoardMixin, TestCase):
    """The strip has to close, and stay closed until something moves again."""

    def setUp(self):
        cache.clear()
        self._vehicle = None
        self.day = timezone.localdate()
        self.reservation = self.make_reservation()
        self.driver = self.make_driver("dis", "Dee")
        self.staff = User.objects.create_user(
            username="dis2", password="x", is_staff=True,
        )
        self.client.force_login(self.staff)
        self.leg_at(time(8, 0), self.driver)
        self.second = self.leg_at(time(14, 0), self.driver)
        apply_pickup_time_move(self.second, time(8, 20), note="Flight match")
        self.html = self.board().content.decode()

    def test_it_closes_without_bootstrap(self):
        # This page loads Bootstrap at the foot of main.html. A strip that
        # will not close is worse than no strip.
        self.assertIn('id="movesDismiss"', self.html)
        self.assertNotIn('data-bs-dismiss="alert"', self.html)
        self.assertIn("banner.hidden = true", self.html)
        self.assertIn("#movesBanner[hidden]{display:none !important}", self.html)

    def test_the_dismissal_is_tied_to_this_refresh(self):
        self.assertIn("data-chg-stamp=", self.html)
        self.assertIn("boardChangesDismissed", self.html)

    def test_a_later_refresh_brings_it_back(self):
        from ops.move_impact import change_set

        before = change_set(self.day).fingerprint
        apply_pickup_time_move(self.second, time(8, 40), note="Flight match")
        after = change_set(self.day).fingerprint
        self.assertNotEqual(before, after, "a new move must beat the dismissal")
