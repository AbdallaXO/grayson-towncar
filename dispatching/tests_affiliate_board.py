"""Affiliate Schedule Board — the ``?view=affiliate`` mode of the schedule board.

Run with:  ./manage.py test dispatching.tests_affiliate_board

What must hold:
  * SEPARATION: the two boards never mix. ``?view=inhouse`` rows are in-house drivers
    only; ``?view=affiliate`` rows are affiliates only. A farmed leg appears on the
    affiliate board, not the in-house one.
  * WHOLE BENCH: EVERY active affiliate gets a row, including ones with zero jobs that
    day (they're the ones you want to farm to). Inactive affiliates stay off.
  * SHARED UNASSIGNED LANE: the unassigned row renders on both boards, so a job is
    farmed by dragging Unassigned -> affiliate row.
  * ASSIGNMENT: the existing update-leg-assignment endpoint farms a leg to an affiliate
    (affiliates are just Drivers, so this is the same front door) and pay auto-fills.
  * FEASIBILITY semantics, the part that is genuinely different:
      - a count_cap/fleet affiliate may run OVERLAPPING jobs (parallel vehicles) and is
        gated only by the daily cap;
      - a single_chain affiliate IS gated by ordinary turnaround/overlap;
      - capability tier and the Port/Sanford pickup permit hard-block;
      - a missing rate card warns but does not block.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import preload_timing_cache
from drivers.models import (AffiliateProfile, Driver, DriverPayRate,
                            DriverVehicleAssignment, FleetVehicle)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=5)


class _AffiliateBoardFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4)
        cls.van = Vehicle.objects.create(
            vehicle_type="van", capacity=12, luggage_capacity=12)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        port = Location.objects.create(name="Port Canaveral")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.port_route = Route.objects.create(
            origin=port, destination=dest, inhouse_base_pay=Decimal("80.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="John", last_name="Doe", email="john@example.com",
            phone_number="5551234567")

        # In-house driver with a towncar assigned for the day.
        cls.sam = Driver.objects.create(
            profile=User.objects.create_user("ab_sam", first_name="Sam"),
            driver_type="inhouse")
        fleet = FleetVehicle.objects.create(
            vehicle_number="T-1", vehicle_type=cls.vehicle, year=2024,
            make="Lincoln", model="Continental")
        DriverVehicleAssignment.objects.create(driver=cls.sam, date=DAY, vehicle=fleet)

        # Waleed — single vehicle, SUV ceiling, no Port/Sanford pickup permit.
        cls.waleed = Driver.objects.create(
            profile=User.objects.create_user("ab_waleed", first_name="Waleed"),
            driver_type="affiliate")
        DriverPayRate.objects.create(driver=cls.waleed, route=cls.route, vehicle=None,
                                     direction="both", base_pay=Decimal("70.00"))
        AffiliateProfile.objects.create(
            driver=cls.waleed, capacity_mode="single_chain",
            max_vehicle_tier="suv", no_pickup_at_port_sanford=True)

        # Anthony — a fleet outfit selling 2 legs/day, parallel vehicles.
        cls.anthony = Driver.objects.create(
            profile=User.objects.create_user("ab_anthony", first_name="Anthony"),
            driver_type="affiliate")
        DriverPayRate.objects.create(driver=cls.anthony, route=cls.route, vehicle=None,
                                     direction="both", base_pay=Decimal("90.00"))
        AffiliateProfile.objects.create(driver=cls.anthony, capacity_mode="count_cap",
                                        daily_cap=2)

        # Nadia — active, no profile and no rate card at all (still belongs on the board).
        cls.nadia = Driver.objects.create(
            profile=User.objects.create_user("ab_nadia", first_name="Nadia"),
            driver_type="affiliate")

        # Retired affiliate — must never render.
        cls.retired = Driver.objects.create(
            profile=User.objects.create_user("ab_old", first_name="Retired"),
            driver_type="affiliate", is_active=False)

        cls.staff = User.objects.create_user("ab_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _leg(self, pickup_time=time(9, 0), driver=None, **kw):
        res_kw = kw.pop("reservation_kw", {})
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=kw.pop("res_vehicle", self.vehicle), base_price=Decimal("100.00"),
            total_price=Decimal("100.00"), **res_kw)
        defaults = dict(
            reservation=res, pickup_date=DAY, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _board(self, view=None):
        url = reverse("schedule_board") + f"?date={DAY.isoformat()}"
        if view:
            url += f"&view={view}"
        return self.client.get(url)

    @staticmethod
    def _row_driver_ids(resp):
        return {r["driver"].id for r in resp.context["inhouse_timeline"]}

    def _feasibility(self, leg, driver):
        return self.client.get(
            reverse("check_driver_feasibility"),
            {"leg_id": leg.id, "driver_id": driver.id},
        ).json()


class BoardSeparationTests(_AffiliateBoardFixture):
    def test_default_view_is_inhouse_and_shows_no_affiliates(self):
        self._leg(driver=self.waleed)
        resp = self._board()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["is_affiliate_board"])
        self.assertEqual(resp.context["board_view"], "inhouse")
        ids = self._row_driver_ids(resp)
        self.assertIn(self.sam.id, ids)
        self.assertNotIn(self.waleed.id, ids)
        self.assertNotIn(self.anthony.id, ids)

    def test_affiliate_view_shows_only_affiliates(self):
        self._leg(driver=self.sam)
        resp = self._board("affiliate")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["is_affiliate_board"])
        ids = self._row_driver_ids(resp)
        self.assertNotIn(self.sam.id, ids)
        self.assertEqual(ids, {self.waleed.id, self.anthony.id, self.nadia.id})

    def test_every_active_affiliate_gets_a_row_even_with_zero_jobs(self):
        # Only Waleed works today; Anthony and Nadia must still be droppable rows.
        self._leg(driver=self.waleed)
        resp = self._board("affiliate")
        rows = {r["driver"].id: r for r in resp.context["inhouse_timeline"]}
        self.assertEqual(rows[self.waleed.id]["total_legs"], 1)
        self.assertEqual(rows[self.anthony.id]["total_legs"], 0)
        self.assertEqual(rows[self.nadia.id]["total_legs"], 0)
        self.assertEqual(resp.context["affiliate_roster_count"], 3)
        self.assertEqual(resp.context["affiliate_working_count"], 1)

    def test_inactive_affiliate_never_renders(self):
        resp = self._board("affiliate")
        self.assertNotIn(self.retired.id, self._row_driver_ids(resp))

    def test_unassigned_lane_renders_on_both_boards(self):
        self._leg()  # no driver
        for view in ("inhouse", "affiliate"):
            resp = self._board(view)
            self.assertEqual(len(resp.context["unassigned_timeline_slots"]), 1,
                             f"unassigned lane missing on {view} board")

    def test_farmed_and_inhouse_counts_split(self):
        self._leg(driver=self.sam)
        self._leg(pickup_time=time(11, 0), driver=self.waleed)
        self._leg(pickup_time=time(13, 0))  # unassigned
        resp = self._board("affiliate")
        self.assertEqual(resp.context["total_legs"], 3)
        self.assertEqual(resp.context["farmed_count"], 1)
        self.assertEqual(resp.context["inhouse_count"], 1)
        self.assertEqual(resp.context["unassigned_count"], 1)

    def test_bad_view_param_falls_back_to_inhouse(self):
        resp = self._board("bogus")
        self.assertEqual(resp.context["board_view"], "inhouse")

    def test_affiliate_board_has_no_available_no_jobs_section(self):
        # Every affiliate is already a row, so the overflow list must be empty.
        resp = self._board("affiliate")
        self.assertEqual(list(resp.context["available_no_jobs"]), [])


class SlotGeometryTests(_AffiliateBoardFixture):
    """Layout maths that silently corrupted the board at scale."""

    def test_job_crossing_midnight_keeps_its_real_duration(self):
        """estimate_job_end_time returns pickup+timedelta, so a late job rolls into
        the next day. Reading .hour off it discarded the date and produced a negative
        offset that floored to the 15-min minimum — every night job drew as a stub."""
        from dispatching.views import _slot_duration_minutes
        from datetime import datetime as _dt
        end = _dt.combine(DAY, time(23, 30)) + timedelta(minutes=75)  # 12:45 AM
        self.assertEqual(end.hour, 0)  # the trap: hour is 0, not 24
        self.assertEqual(_slot_duration_minutes(DAY, time(23, 30), end), 75)

    def test_normal_daytime_duration_unaffected(self):
        from dispatching.views import _slot_duration_minutes
        from datetime import datetime as _dt
        end = _dt.combine(DAY, time(9, 0)) + timedelta(minutes=50)
        self.assertEqual(_slot_duration_minutes(DAY, time(9, 0), end), 50)

    def test_duration_floors_at_fifteen_minutes(self):
        from dispatching.views import _slot_duration_minutes
        from datetime import datetime as _dt
        end = _dt.combine(DAY, time(9, 0)) + timedelta(minutes=3)
        self.assertEqual(_slot_duration_minutes(DAY, time(9, 0), end), 15)

    def test_lane_packing_uses_true_concurrency_not_input_order(self):
        """Greedy packing is only correct in START order. Packing in display order
        (vehicle class first) parked each lane cursor late, so the next group's
        early job appended a new lane — lane count became the SUM of per-group
        peaks instead of the day's actual peak."""
        from dispatching.views import _pack_lanes
        # Three non-overlapping jobs, fed in an adversarial (reverse) order.
        slots = [
            {"position_pct": 60.0, "width_pct": 10.0},
            {"position_pct": 30.0, "width_pct": 10.0},
            {"position_pct": 0.0, "width_pct": 10.0},
        ]
        self.assertEqual(_pack_lanes(slots, lane_height=18, gap=2), 1)
        self.assertTrue(all(s["lane"] == 0 for s in slots))

    def test_lane_packing_stacks_genuine_overlaps(self):
        from dispatching.views import _pack_lanes
        slots = [
            {"position_pct": 0.0, "width_pct": 20.0},
            {"position_pct": 5.0, "width_pct": 20.0},
            {"position_pct": 10.0, "width_pct": 20.0},
        ]
        self.assertEqual(_pack_lanes(slots, lane_height=30, gap=2), 3)
        self.assertEqual(sorted(s["lane"] for s in slots), [0, 1, 2])
        self.assertEqual(sorted(s["lane_top"] for s in slots), [2, 34, 66])

    def test_overlapping_driver_jobs_get_distinct_lanes(self):
        """The board-level guarantee: two concurrent jobs on one driver must not
        share a top offset, or the earlier one is buried and undraggable."""
        self._leg(pickup_time=time(9, 0), driver=self.sam)
        self._leg(pickup_time=time(9, 15), driver=self.sam)
        resp = self._board()
        row = {r["driver"].id: r for r in resp.context["inhouse_timeline"]}[self.sam.id]
        tops = [s.lane_top for s in row["schedule"].slots]
        self.assertEqual(len(tops), 2)
        self.assertNotEqual(tops[0], tops[1], "concurrent jobs share a lane — one is hidden")
        self.assertTrue(row["has_overlap"])
        self.assertGreater(row["row_bar_height"], 34)

    def test_sequential_driver_jobs_stay_on_one_lane(self):
        self._leg(pickup_time=time(8, 0), driver=self.sam)
        self._leg(pickup_time=time(16, 0), driver=self.sam)
        resp = self._board()
        row = {r["driver"].id: r for r in resp.context["inhouse_timeline"]}[self.sam.id]
        self.assertEqual({s.lane for s in row["schedule"].slots}, {0})
        self.assertFalse(row["has_overlap"])


class SlotNotesTests(_AffiliateBoardFixture):
    """Notes surface on hover. The four fields mean different things (dispatcher note,
    internal booking note, guest request, driver's write-back) so they stay distinct,
    and an absent one must render nothing rather than an empty row."""

    def _slot(self, leg, resp=None):
        resp = resp or self._board()
        for row in resp.context["inhouse_timeline"]:
            for s in row["schedule"].slots:
                if s.leg_id == leg.id:
                    return s
        raise AssertionError("slot not found")

    def test_all_note_sources_are_collected_separately(self):
        leg = self._leg(driver=self.sam, reservation_kw={
            "private_notes": "Internal booking note",
            "special_requests": "Two waters please",
        })
        leg.private_notes = "Meet inside baggage claim"
        leg.driver_notes = "Guest ran late last time"
        leg.save()
        s = self._slot(leg)
        self.assertEqual(s.note_leg, "Meet inside baggage claim")
        self.assertEqual(s.note_res, "Internal booking note")
        self.assertEqual(s.note_guest, "Two waters please")
        self.assertEqual(s.note_driver, "Guest ran late last time")
        self.assertTrue(s.has_notes)

    def test_leg_with_no_notes_reports_none(self):
        leg = self._leg(driver=self.sam)
        s = self._slot(leg)
        self.assertFalse(s.has_notes)
        self.assertEqual(
            [s.note_leg, s.note_res, s.note_guest, s.note_driver, s.note_stops],
            ["", "", "", "", ""])

    def test_whitespace_only_note_does_not_count_as_a_note(self):
        leg = self._leg(driver=self.sam)
        leg.private_notes = "   \n  "
        leg.save()
        s = self._slot(leg)
        self.assertEqual(s.note_leg, "")
        self.assertFalse(s.has_notes, "a blank note must not flag the bar")

    def test_stop_notes_are_labelled_with_their_location(self):
        from reservations.models import LegStop
        leg = self._leg(driver=self.sam)
        LegStop.objects.create(leg=leg, sequence=1, location_text="Publix Turkey Lake",
                               notes="Grocery run, 15 min max")
        s = self._slot(leg)
        self.assertIn("Grocery run, 15 min max", s.note_stops)
        self.assertIn("Publix Turkey Lake", s.note_stops)
        self.assertTrue(s.has_notes)

    def test_stop_without_notes_is_skipped(self):
        from reservations.models import LegStop
        leg = self._leg(driver=self.sam)
        LegStop.objects.create(leg=leg, sequence=1, location_text="Somewhere", notes="")
        self.assertEqual(self._slot(leg).note_stops, "")

    def test_unassigned_chips_carry_notes_too(self):
        leg = self._leg(reservation_kw={"special_requests": "Anniversary trip"})
        resp = self._board()
        chip = next(c for c in resp.context["unassigned_timeline_slots"]
                    if c["leg_id"] == leg.id)
        self.assertEqual(chip["note_guest"], "Anniversary trip")
        self.assertTrue(chip["has_notes"])

    def test_notes_render_into_the_slot_markup(self):
        leg = self._leg(driver=self.sam)
        leg.private_notes = "Meet inside baggage claim"
        leg.save()
        resp = self._board()
        self.assertContains(resp, "data-note-leg=\"Meet inside baggage claim\"")
        self.assertContains(resp, "has-note")

    def test_note_markup_is_escaped(self):
        """Notes are free text — a quote or angle bracket must not break the
        attribute or inject markup."""
        leg = self._leg(driver=self.sam)
        leg.private_notes = 'He said "go" <script>alert(1)</script>'
        leg.save()
        resp = self._board()
        self.assertNotContains(resp, "<script>alert(1)</script>")
        self.assertContains(resp, "&quot;go&quot;")


class BoardClockTests(_AffiliateBoardFixture):
    """The clock is seeded server-side so the board reads company-local time
    regardless of the viewer's machine, and the now-line only claims a position
    on a day where 'now' actually falls."""

    def test_clock_seed_is_server_local_time(self):
        before = timezone.localtime()
        resp = self._board()
        after = timezone.localtime()
        secs = resp.context["board_now_secs"]

        def as_secs(dt):
            return dt.hour * 3600 + dt.minute * 60 + dt.second

        # Guard against a midnight-rollover false failure.
        if before.date() == after.date():
            self.assertGreaterEqual(secs, as_secs(before))
            self.assertLessEqual(secs, as_secs(after))
        self.assertGreaterEqual(secs, 0)
        self.assertLess(secs, 86400)

    def test_now_marker_suppressed_on_a_non_today_board(self):
        # DAY is 5 days out, so the board must not draw a "now" line.
        resp = self._board()
        self.assertFalse(resp.context["board_is_today"])
        self.assertNotContains(resp, 'id="timelineNowLine"')

    def test_now_marker_renders_on_todays_board(self):
        today = timezone.localdate()
        Leg.objects.create(
            reservation=Reservation.objects.create(
                trip_type="one-way", customer=self.customer, rate=self.rate,
                vehicle=self.vehicle, base_price=Decimal("100.00"),
                total_price=Decimal("100.00")),
            pickup_date=today, pickup_time=time(9, 0), pickup_location="MCO",
            dropoff_location="Disney", route=self.route, status="confirmed")
        resp = self.client.get(
            reverse("schedule_board") + f"?date={today.isoformat()}")
        self.assertTrue(resp.context["board_is_today"])
        self.assertContains(resp, 'id="timelineNowLine"')

    def test_clock_renders_on_both_boards(self):
        for view in ("inhouse", "affiliate"):
            self.assertContains(self._board(view), 'id="boardClockTime"')

    def test_completed_slots_emit_the_dimming_hook(self):
        """Completed jobs are dimmed by a [data-status="completed"] CSS rule, so
        the attribute is the contract — assert the board actually emits it."""
        self._leg(pickup_time=time(9, 0), driver=self.sam, status="completed")
        resp = self._board()
        self.assertContains(resp, 'data-status="completed"')

    def test_timeline_geometry_exposed_for_the_now_line(self):
        """The JS re-derives slot placement, so it needs the same origin/width
        the server used."""
        self._leg(pickup_time=time(9, 0))
        resp = self._board()
        start = resp.context["board_display_start"]
        total = resp.context["board_total_minutes"]
        self.assertEqual(total % 60, 0)
        self.assertEqual(total // 60, resp.context["timeline_hours"][-1] - start + 1)
        self.assertLessEqual(start, 9, "the only job must fall inside the window")


class AdaptiveAxisTests(_AffiliateBoardFixture):
    """The axis fits the day instead of forcing a 6am-10pm floor on every date.
    A light day used to render as an unreadable 2%-wide cluster in 17 hours of white."""

    def _axis(self, resp):
        start = resp.context["board_display_start"]
        return start, start + resp.context["board_total_minutes"] // 60 - 1

    def test_sparse_day_gets_a_tight_axis(self):
        for _ in range(5):
            self._leg(pickup_time=time(9, 0))
        start, end = self._axis(self._board())
        self.assertLessEqual(end - start, 8, "a one-hour day should not span 17 hours")
        self.assertLess(start, 9)
        self.assertGreater(end, 9)

    def test_axis_never_collapses_below_the_minimum_span(self):
        self._leg(pickup_time=time(9, 0))
        start, end = self._axis(self._board())
        self.assertGreaterEqual(end - start, 5)

    def test_busy_day_still_spans_the_whole_operating_window(self):
        for h in (5, 8, 11, 14, 17, 20, 22):
            self._leg(pickup_time=time(h, 0))
        start, end = self._axis(self._board())
        self.assertLessEqual(start, 5)
        self.assertGreaterEqual(end, 22)

    def test_axis_stays_within_a_single_day(self):
        self._leg(pickup_time=time(23, 30))
        start, end = self._axis(self._board())
        self.assertGreaterEqual(start, 0)
        self.assertLessEqual(end, 23)

    def test_empty_day_falls_back_to_business_hours(self):
        start, end = self._axis(self._board())
        self.assertEqual((start, end), (6, 22))


class DriverRowPresenceTests(_AffiliateBoardFixture):
    """Every active driver must be a drop target. The old rule hid drivers with
    neither a vehicle nor a job into a context key the template never rendered —
    so any date before Day Setup ran had NOTHING to assign onto."""

    def test_future_date_before_day_setup_still_has_drop_targets(self):
        future = DAY + timedelta(days=30)  # no DriverVehicleAssignment exists here
        leg = Leg.objects.create(
            reservation=Reservation.objects.create(
                trip_type="one-way", customer=self.customer, rate=self.rate,
                vehicle=self.vehicle, base_price=Decimal("100.00"),
                total_price=Decimal("100.00")),
            pickup_date=future, pickup_time=time(9, 0), pickup_location="MCO",
            dropoff_location="Disney", route=self.route, status="confirmed")
        resp = self.client.get(
            reverse("schedule_board") + f"?date={future.isoformat()}")
        rows = resp.context["inhouse_timeline"]
        self.assertIn(self.sam.id, {r["driver"].id for r in rows},
                      "no driver rows on a future date — nothing to drag onto")
        self.assertEqual(resp.context["unassigned_count"], 1)
        self.assertEqual(len(leg.reservation.legs.all()), 1)

    def test_driver_with_neither_vehicle_nor_jobs_still_renders(self):
        spare = Driver.objects.create(
            profile=User.objects.create_user("ab_spare", first_name="Spare"),
            driver_type="inhouse")
        resp = self._board()
        self.assertIn(spare.id, {r["driver"].id for r in resp.context["inhouse_timeline"]})

    def _set_off(self, driver):
        from drivers.models import DriverDateOverride
        DriverDateOverride.objects.create(
            driver=driver, date=DAY, exception_type="off", status="approved")

    def test_driver_who_is_off_is_hidden(self):
        off = Driver.objects.create(
            profile=User.objects.create_user("ab_off", first_name="Offduty"),
            driver_type="inhouse")
        self._set_off(off)
        resp = self._board()
        self.assertNotIn(off.id, {r["driver"].id for r in resp.context["inhouse_timeline"]})

    def test_off_driver_holding_jobs_is_still_shown(self):
        """Hiding them would take their assigned legs off the board too — the work
        would vanish instead of surfacing as the conflict it is."""
        off = Driver.objects.create(
            profile=User.objects.create_user("ab_off2", first_name="Offbusy"),
            driver_type="inhouse")
        self._set_off(off)
        self._leg(pickup_time=time(10, 0), driver=off)
        resp = self._board()
        rows = {r["driver"].id: r for r in resp.context["inhouse_timeline"]}
        self.assertIn(off.id, rows, "an off driver's assigned jobs disappeared")
        self.assertEqual(rows[off.id]["total_legs"], 1)

    def test_vehicle_numbers_sort_numerically_in_the_real_fleet_format(self):
        """The fleet is numbered 001..009 then 10, 11, 12... Zero-padded and bare
        numbers must interleave by VALUE, so 009 precedes 10. Plain string order
        would give 001, 009, 10, 100, 11 — digits compared character by character."""
        from drivers.models import FleetVehicle
        fleet = ["001", "002", "003", "009", "10", "11", "12", "13", "14"]
        # Create in shuffled order so a passing result can't come from insertion order.
        for num in ["12", "001", "14", "009", "10", "003", "13", "002", "11"]:
            d = Driver.objects.create(
                profile=User.objects.create_user(f"ab_v{num}", first_name=f"Zdrv{num}"),
                driver_type="inhouse")
            fv = FleetVehicle.objects.create(
                vehicle_number=num, vehicle_type=self.vehicle, year=2024,
                make="Ford", model="Transit")
            DriverVehicleAssignment.objects.create(driver=d, date=DAY, vehicle=fv)
        resp = self._board()
        nums = [r["vehicle_number"] for r in resp.context["inhouse_timeline"]
                if r["vehicle_number"] in fleet]
        self.assertEqual(nums, fleet)

    def test_no_vehicle_group_is_marked_once_for_the_divider(self):
        """Deployed drivers and available-but-not-set-up drivers are both drop
        targets but mean different things, so the board splits them with a single
        divider. Exactly one row may carry the marker."""
        for fn in ("Zeta", "Alpha"):
            Driver.objects.create(
                profile=User.objects.create_user(f"ab_g{fn}", first_name=fn),
                driver_type="inhouse")
        rows = self._board().context["inhouse_timeline"]
        marked = [i for i, r in enumerate(rows) if r.get("starts_no_vehicle_group")]
        self.assertEqual(len(marked), 1, "divider must appear exactly once")
        # It must sit on the first row of the no-vehicle group.
        self.assertFalse(rows[marked[0]]["has_vehicle"])
        self.assertTrue(all(r["has_vehicle"] for r in rows[:marked[0]]))
        self.assertTrue(all(not r["has_vehicle"] for r in rows[marked[0]:]))

    def test_no_divider_when_every_driver_has_a_vehicle(self):
        # self.sam is the only in-house driver and he has a vehicle for DAY.
        rows = self._board().context["inhouse_timeline"]
        self.assertTrue(all(r["has_vehicle"] for r in rows))
        self.assertFalse(any(r.get("starts_no_vehicle_group") for r in rows))

    def test_affiliate_board_never_draws_the_vehicle_divider(self):
        """Affiliates hold no fleet vehicle at all, so the split is meaningless
        there — marking it would put a divider above the very first row."""
        rows = self._board("affiliate").context["inhouse_timeline"]
        self.assertFalse(any(r.get("starts_no_vehicle_group") for r in rows))

    def test_no_vehicle_row_is_tagged_in_the_markup(self):
        Driver.objects.create(
            profile=User.objects.create_user("ab_tag", first_name="Untagged"),
            driver_type="inhouse")
        resp = self._board()
        self.assertContains(resp, "no-vehicle-tag")
        self.assertContains(resp, "Available — no vehicle assigned")

    def test_drivers_without_vehicles_sort_after_and_alphabetically(self):
        for fn in ("Zeta", "Alpha"):
            Driver.objects.create(
                profile=User.objects.create_user(f"ab_nv{fn}", first_name=fn),
                driver_type="inhouse")
        rows = self._board().context["inhouse_timeline"]
        with_veh = [i for i, r in enumerate(rows) if r["vehicle_number"]]
        no_veh = [i for i, r in enumerate(rows) if not r["vehicle_number"]]
        self.assertLess(max(with_veh), min(no_veh), "vehicle-assigned must come first")
        names = [str(rows[i]["driver"]) for i in no_veh]
        self.assertEqual(names, sorted(names))


class AffiliateRowMetadataTests(_AffiliateBoardFixture):
    def _row(self, driver, resp=None):
        resp = resp or self._board("affiliate")
        return {r["driver"].id: r for r in resp.context["inhouse_timeline"]}[driver.id]

    def test_count_cap_row_reports_usage(self):
        self._leg(driver=self.anthony)
        row = self._row(self.anthony)
        self.assertEqual(row["aff_cap_label"], "Cap 1/2")
        self.assertFalse(row["aff_cap_full"])

    def test_count_cap_row_flags_full(self):
        self._leg(driver=self.anthony)
        self._leg(pickup_time=time(15, 0), driver=self.anthony)
        row = self._row(self.anthony)
        self.assertEqual(row["aff_cap_label"], "Cap 2/2")
        self.assertTrue(row["aff_cap_full"])

    def test_single_chain_row_labels_single_vehicle(self):
        row = self._row(self.waleed)
        self.assertEqual(row["aff_cap_label"], "Single vehicle")
        self.assertTrue(row["aff_no_port"])
        self.assertFalse(row["aff_no_rate"])

    def test_profileless_uncarded_affiliate_is_flagged_not_hidden(self):
        row = self._row(self.nadia)
        self.assertEqual(row["aff_cap_label"], "No profile")
        self.assertTrue(row["aff_no_rate"])

    def test_inhouse_rows_carry_no_affiliate_metadata(self):
        self._leg(driver=self.sam)
        resp = self._board("inhouse")
        row = {r["driver"].id: r for r in resp.context["inhouse_timeline"]}[self.sam.id]
        self.assertEqual(row["aff_cap_label"], "")
        self.assertFalse(row["aff_no_rate"])


class AffiliateAssignmentTests(_AffiliateBoardFixture):
    def test_dragging_unassigned_to_affiliate_farms_the_leg_and_fills_pay(self):
        leg = self._leg()
        resp = self.client.post(
            reverse("update_leg_assignment"),
            {"leg_id": leg.id, "field": "driver", "value": str(self.waleed.id)},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["success"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.waleed.id)
        self.assertEqual(leg.driver_base_pay, Decimal("70.00"))

    def test_farmed_leg_moves_between_the_two_boards(self):
        leg = self._leg(driver=self.sam)
        self.assertIn(leg.id, self._slot_ids(self._board("inhouse")))
        leg.driver = self.waleed
        leg.save()
        self.assertNotIn(leg.id, self._slot_ids(self._board("inhouse")))
        self.assertIn(leg.id, self._slot_ids(self._board("affiliate")))

    @staticmethod
    def _slot_ids(resp):
        return {s.leg_id
                for row in resp.context["inhouse_timeline"]
                for s in row["schedule"].slots}


class AffiliateFeasibilityTests(_AffiliateBoardFixture):
    def test_fleet_affiliate_allows_overlapping_jobs(self):
        """The whole point of count_cap/fleet: parallel vehicles, so a job that
        overlaps an existing one is NOT a conflict."""
        self._leg(pickup_time=time(9, 0), driver=self.anthony)
        overlapping = self._leg(pickup_time=time(9, 15))
        body = self._feasibility(overlapping, self.anthony)
        self.assertTrue(body["feasible"], body)
        self.assertEqual(body["capacity_mode"], "count_cap")
        self.assertIn("1/2", body["reason"])

    def test_fleet_affiliate_blocked_at_daily_cap(self):
        self._leg(pickup_time=time(9, 0), driver=self.anthony)
        self._leg(pickup_time=time(14, 0), driver=self.anthony)
        third = self._leg(pickup_time=time(18, 0))
        body = self._feasibility(third, self.anthony)
        self.assertFalse(body["feasible"], body)
        self.assertIn("daily cap", body["reason"].lower())

    def test_single_chain_affiliate_still_catches_overlap(self):
        """A one-vehicle affiliate gets ordinary chain feasibility — an overlapping
        job is a real double-book."""
        self._leg(pickup_time=time(9, 0), driver=self.waleed)
        overlapping = self._leg(pickup_time=time(9, 15))
        body = self._feasibility(overlapping, self.waleed)
        self.assertFalse(body["feasible"], body)

    def test_vehicle_tier_ceiling_blocks(self):
        van_leg = self._leg(res_vehicle=self.van)
        body = self._feasibility(van_leg, self.waleed)  # Waleed tops out at SUV
        self.assertFalse(body["feasible"], body)
        self.assertIn("tops out", body["reason"].lower())

    def test_port_pickup_permit_blocks(self):
        port_leg = self._leg(pickup_location="Port Canaveral",
                             route=self.port_route)
        body = self._feasibility(port_leg, self.waleed)
        self.assertFalse(body["feasible"], body)
        self.assertIn("permit", body["reason"].lower())

    def test_missing_rate_card_warns_but_does_not_block(self):
        leg = self._leg()
        body = self._feasibility(leg, self.nadia)
        self.assertTrue(body["feasible"], body)
        self.assertTrue(any("rate card" in w.lower() for w in body["warnings"]), body)

    def test_completed_legs_still_consume_a_daily_seat(self):
        """A finished trip used a vehicle. Counting only 'active' legs (the in-house
        rule) would hand out a seat the Farm-Out apply path then refuses."""
        self._leg(pickup_time=time(9, 0), driver=self.anthony, status="completed")
        self._leg(pickup_time=time(14, 0), driver=self.anthony)
        third = self._leg(pickup_time=time(18, 0))
        body = self._feasibility(third, self.anthony)
        self.assertFalse(body["feasible"], body)
        self.assertIn("daily cap", body["reason"].lower())

    def test_cancelled_legs_do_not_consume_a_seat(self):
        self._leg(pickup_time=time(9, 0), driver=self.anthony, status="cancelled")
        self._leg(pickup_time=time(14, 0), driver=self.anthony)
        third = self._leg(pickup_time=time(18, 0))
        body = self._feasibility(third, self.anthony)
        self.assertTrue(body["feasible"], body)

    def test_row_badge_agrees_with_the_drop_check(self):
        """The badge must never promise room the drop check refuses."""
        self._leg(pickup_time=time(9, 0), driver=self.anthony, status="completed")
        self._leg(pickup_time=time(14, 0), driver=self.anthony)
        resp = self._board("affiliate")
        row = {r["driver"].id: r for r in resp.context["inhouse_timeline"]}[self.anthony.id]
        self.assertEqual(row["aff_cap_label"], "Cap 2/2")
        self.assertTrue(row["aff_cap_full"])
        body = self._feasibility(self._leg(pickup_time=time(18, 0)), self.anthony)
        self.assertFalse(body["feasible"], body)

    def test_affiliate_never_reports_a_vehicle_mismatch(self):
        """Affiliates hold no DriverVehicleAssignment; the in-house check would
        otherwise fail every affiliate with 'no vehicle assigned today'."""
        leg = self._leg()
        body = self._feasibility(leg, self.anthony)
        self.assertTrue(body["vehicle_match"], body)
        self.assertEqual(body["vehicle_mismatch_detail"], "")
