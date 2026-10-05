"""The engine reads a confirmed regular shift to the minute (structured shifts, Stage 1).

Task 3c: check_feasibility holds every regular-shift day to its shape's max_span_min,
measured base -> base (leaving base for the first pickup to back at base after the last
clear). That is what keeps Float and "Morning or Evening" days — whose window runs from
the earliest start to the latest end — at 12 hours.

Task 4b: base -> base counts only the drive in Stage 1 (07 §13 K10), and a day already
over 12h takes no further planned leg, even one that fits inside it (S20).

Task 5: every engine path carries the regular window. With the switch off nothing does
(no "source" ever reaches the rules door); with it on, auto-assign (modal and saved
hours), the capacity planner, the conflict advisor and the day planner all read a
confirmed regular shift to the minute, base to base, and bypass the stub. A dispatcher's
own moves onto a regular day already over 12h only warn (S20, §6.5).

Driver 46 is in the observed-history stub (06-20, 14h, non-flexible here), so a late job
is refused unless his regular shift bypasses it. Trip ends are pinned to pickup + 90
minutes (the tests_fleet_day trick). Lead / tail, drive-only (Task 4b): MCO pickup 22,
Disney pickup 50; back to base 12 from an MCO drop, 35 from a Disney drop.

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_regular_shift_engine
"""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

import dispatching.scheduler as sch
from dispatching import assignment_pipeline as ap
from dispatching import feasibility_guards as fg
from dispatching.scheduler import check_feasibility
from dispatching.tests_fleet_day import _ninety_minutes
from dispatching.tests_founder_brain import D, _leg, _sched, _slot
from drivers import regular_shifts as rs
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle, ShiftTemplate
from drivers.regular_shifts import DayShift
from drivers.test_support import RegularShiftCacheMixin
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation


def _at(h, m):
    return datetime.combine(D, time(h, m))


class BaseSpanFeasibilityTests(RegularShiftCacheMixin, TestCase):
    """check_feasibility passes the day's base -> base span, with and without the new leg."""

    FLOAT = {"start": 3, "end": 23, "start_min": 180, "end_min": 1575, "kind": "float",
             "source": "regular", "max_span_min": 720, "max_hours": None, "flexible": False}

    def test_check_feasibility_float_day_held_to_12h(self):
        # Leave base 04:38 for the 05:00 MCO arrival; the 14:00 departure clears 15:00 at MCO,
        # back at base 15:12 (10h 34m). A 16:30 Disney departure clearing 17:20 at MCO puts
        # him back at 17:32 — 12h 54m base to base.
        day = _sched(46, [
            _slot(_leg(1, 5, 0, trip="arrival", pickup_loc="MCO Terminal",
                       dropoff_loc="Disney Resort"), end_dt=_at(6, 15)),
            _slot(_leg(2, 14, 0, trip="departure", pickup_loc="Disney Resort",
                       dropoff_loc="MCO Terminal"), end_dt=_at(15, 0)),
        ])
        late = _leg(3, 16, 30, trip="departure", pickup_loc="Disney Resort",
                    dropoff_loc="MCO Terminal")
        no_cap = {k: v for k, v in self.FLOAT.items() if k != "max_span_min"}
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(17, 20)):
            refused = check_feasibility(day, late, D, driver_window=self.FLOAT)
            allowed = check_feasibility(day, late, D, driver_window=no_cap)
        self.assertFalse(refused.feasible)
        self.assertEqual(refused.reason, "Outside driver window: base to base 12h 54m > 12h 0m")
        self.assertTrue(allowed.feasible, allowed.reason)

    def test_check_feasibility_day_already_over_takes_no_more_planned_work(self):
        # S20: a board built by hand that is already past 12h — leave base 04:38 for the
        # 05:00 MCO arrival; the 17:00 departure clears 17:40 at MCO, back at base 17:52
        # (13h 14m). Neither a leg inside that span (it leaves the span unchanged) nor one
        # that ends later is planned onto it. The hole-fill only gets the "already over"
        # reason if check_feasibility passes the span WITHOUT the new leg; with none it
        # would read as "base to base 13h 14m".
        day = _sched(47, [
            _slot(_leg(1, 5, 0, trip="arrival", pickup_loc="MCO Terminal",
                       dropoff_loc="Disney Resort"), end_dt=_at(6, 15)),
            _slot(_leg(2, 17, 0, trip="departure", pickup_loc="Disney Resort",
                       dropoff_loc="MCO Terminal"), end_dt=_at(17, 40)),
        ])
        hole = _leg(3, 10, 0, trip="other", pickup_loc="Disney Resort",
                    dropoff_loc="Disney Resort")
        later = _leg(4, 19, 0, trip="departure", pickup_loc="Disney Resort",
                     dropoff_loc="MCO Terminal")
        no_cap = {k: v for k, v in self.FLOAT.items() if k != "max_span_min"}
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(10, 40)):
            hole_refused = check_feasibility(day, hole, D, driver_window=self.FLOAT)
            hole_allowed = check_feasibility(day, hole, D, driver_window=no_cap)
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(19, 40)):
            later_refused = check_feasibility(day, later, D, driver_window=self.FLOAT)
            later_allowed = check_feasibility(day, later, D, driver_window=no_cap)
        for refused in (hole_refused, later_refused):
            self.assertFalse(refused.feasible)
            self.assertEqual(refused.reason,
                             "Outside driver window: day already over 12h 0m base to base")
        self.assertTrue(hole_allowed.feasible, hole_allowed.reason)
        self.assertTrue(later_allowed.feasible, later_allowed.reason)

    def test_manual_sovereign_regular_window_only_warns_on_span(self):
        # The same over-12h board, through the manual-sovereign door (enforce_cap=False):
        # the ceiling rides as span_warn_min, so both legs are allowed and the S20 reason is
        # a warning. The engine's door (enforce_cap=True) keeps max_span_min and refuses.
        day = _sched(47, [
            _slot(_leg(1, 5, 0, trip="arrival", pickup_loc="MCO Terminal",
                       dropoff_loc="Disney Resort"), end_dt=_at(6, 15)),
            _slot(_leg(2, 17, 0, trip="departure", pickup_loc="Disney Resort",
                       dropoff_loc="MCO Terminal"), end_dt=_at(17, 40)),
        ])
        hole = _leg(3, 10, 0, trip="other", pickup_loc="Disney Resort",
                    dropoff_loc="Disney Resort")
        manual = fg.get_effective_window(47, configured=self.FLOAT, enforce_cap=False)
        engine = fg.get_effective_window(47, configured=self.FLOAT)
        self.assertNotIn("max_span_min", manual)
        self.assertEqual(manual["span_warn_min"], 720)
        self.assertEqual(engine["max_span_min"], 720)
        self.assertNotIn("span_warn_min", engine)
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(10, 40)):
            warned = check_feasibility(day, hole, D, driver_window=manual)
            refused = check_feasibility(day, hole, D, driver_window=engine)
        self.assertTrue(warned.feasible, warned.reason)
        self.assertIn("day already over 12h 0m base to base", warned.warnings)
        self.assertFalse(refused.feasible)
        # A day the move itself takes over 12h warns with the span it reaches.
        short = _sched(47, day.slots[:1])
        late = _leg(4, 17, 0, trip="departure", pickup_loc="Disney Resort",
                    dropoff_loc="MCO Terminal")
        with mock.patch.object(sch, "estimate_job_end_time", return_value=_at(17, 40)):
            warned = check_feasibility(short, late, D, driver_window=manual)
        self.assertTrue(warned.feasible, warned.reason)
        self.assertIn("base to base 13h 14m > 12h 0m", warned.warnings)

    def test_only_a_ceiling_is_renamed(self):
        # Retyped modal hours (S9) are a regular window with no ceiling: nothing to rename.
        retyped = {"start": 4, "end": 22, "max_hours": None, "flexible": False,
                   "source": "regular"}
        self.assertEqual(fg.get_effective_window(46, configured=retyped, enforce_cap=False),
                         dict(retyped, night_exempt=True))


# ════════════════════════════════════════════════════════════════════════════
# The engine's paths, end to end on a real board
# ════════════════════════════════════════════════════════════════════════════

DAY = timezone.localdate() + timedelta(days=9)    # every weekday is confirmed alike
MCO = "MCO Terminal B"                           # categorize_location -> "MCO Terminal"
DISNEY = "Disney's Contemporary Resort"          # categorize_location -> "Disney Resort"
DISNEY_2 = "Disney's Polynesian Village Resort"  # categorize_location -> "Disney Resort"
EVENING = ("evening", time(14, 15), time(2, 15))   # window (855, 1575)
MORNING = ("morning", time(4, 35), time(15, 35))   # window (275, 995)
FLOAT = ("float", None, None)                      # window (180, 1575), 12h base to base


class _EngineFixture(RegularShiftCacheMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.route = Route.objects.create(origin=Location.objects.create(name="MCO"),
                                         destination=Location.objects.create(name="Disney"),
                                         inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(vehicle=cls.suv, route=cls.route,
                                       oneway_price=Decimal("100.00"),
                                       round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(first_name="John", last_name="Doe",
                                               email="j@example.com", phone_number="5551234567")
        cls.staff = User.objects.create_user("rse_staff", password="x", is_staff=True)
        cls.manager = User.objects.create_user("rse_manager", is_staff=True, is_superuser=True)
        # Stubbed 06-20, 14h; saved hours 06-23, not flexible; no weekly rows.
        cls.d46 = cls.driver(46, "Yovanny", default_flexible=False,
                             default_start_hour=6, default_end_hour=23)

    @classmethod
    def driver(cls, pk, first, **kw):
        driver = Driver.objects.create(
            pk=pk, profile=User.objects.create_user(f"rse_{first.lower()}", first_name=first),
            driver_type="inhouse", **kw)
        unit = FleetVehicle.objects.create(vehicle_number=str(pk), vehicle_type=cls.suv,
                                           year=2024, make="Chevrolet", model="Suburban")
        DriverVehicleAssignment.objects.create(driver=driver, vehicle=unit, date=DAY)
        return driver

    def setUp(self):
        super().setUp()
        ends = mock.patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
        ends.start()
        self.addCleanup(ends.stop)
        cache.clear()   # the capacity planner caches its suggestions per date for 60s
        self.client.force_login(self.staff)
        self.t = {t.kind: t for t in ShiftTemplate.objects.all()}

    # ── fixtures ──
    def leg(self, hh, mm=0, pickup=MCO, dropoff=DISNEY, driver=None):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate, vehicle=self.suv,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=time(hh, mm), driver=driver,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route,
            status="confirmed")

    def confirm(self, shape, driver=None):
        """Confirm the same regular day every day of the week; returns a fresh driver."""
        driver = driver or self.d46
        kind, start, end = shape
        tid = self.t[kind].id
        rs.save_regular_shift(driver, [DayShift(i, tid, start, end) for i in range(7)],
                              self.manager, role_template_id=tid)
        return Driver.objects.get(pk=driver.pk)

    def switch_on(self):
        with self.assertLogs("drivers.regular_shifts", "INFO"):
            self.assertEqual(rs.set_regular_windows(True, self.manager), (True, ""))

    def switch_off(self):
        with self.assertLogs("drivers.regular_shifts", "INFO"):
            self.assertEqual(rs.set_regular_windows(False, self.manager), (True, ""))

    # ── auto-assign ──
    def modal(self, *drivers):
        """The Auto-Assign modal as it opens: each driver's saved hours, untouched."""
        out = {}
        for d in drivers:
            eff = Driver.objects.get(pk=d.pk).get_effective_availability(DAY)
            out[d.pk] = (eff["start_hour"], eff["end_hour"])
        return out

    def preview(self, hours=None):
        """auto_assign_drivers in preview mode. hours {driver_id: (start, end)} is the
        modal payload; None posts none, so each driver's saved hours are used."""
        body = {"date": DAY.isoformat(), "apply": False}
        if hours is not None:
            body["driver_hours"] = {str(did): {"start": s, "end": e, "flexible": False}
                                    for did, (s, e) in hours.items()}
        resp = self.client.post(reverse("auto_assign_drivers"), data=json.dumps(body),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def assigned(self, body):
        """{leg_id: driver_id} the preview proposes."""
        return {s["leg_id"]: ds["driver_id"] for ds in body["driver_schedules"]
                for s in ds["slots"] if s["is_new"]}


class AutoAssignRegularShiftTests(_EngineFixture):
    def test_switch_off_no_regular_keys_reach_guards(self):
        self.leg(9, 0)
        self.leg(22, 30, pickup=DISNEY, dropoff=MCO)
        before = (self.preview(self.modal(self.d46)), self.preview())
        self.confirm(MORNING)
        with mock.patch.object(fg, "get_effective_window",
                               wraps=fg.get_effective_window) as spy:
            after = (self.preview(self.modal(self.d46)), self.preview())
        self.assertTrue(spy.call_args_list)
        for call in spy.call_args_list:
            configured = call.kwargs.get("configured",
                                         call.args[1] if len(call.args) > 1 else None)
            self.assertFalse(configured and "source" in configured, configured)
        # Confirming changes nothing the build can see while the switch is off.
        self.assertEqual(before, after)

    def test_evening_regular_shift_takes_late_job(self):
        late = self.leg(22, 30, pickup=DISNEY, dropoff=MCO)   # clears 00:00, base 00:12
        self.confirm(EVENING)
        # Switch off: the stub's 20:00 clear-by refuses a job clearing at midnight.
        self.assertNotIn(late.id, self.assigned(self.preview(self.modal(self.d46))))
        self.assertNotIn(late.id, self.assigned(self.preview()))
        self.switch_on()
        self.assertEqual(self.modal(self.d46), {46: (14, 23)})
        self.assertEqual(self.assigned(self.preview(self.modal(self.d46))), {late.id: 46})
        self.assertEqual(self.assigned(self.preview()), {late.id: 46})

    def test_evening_regular_shift_refuses_early_job(self):
        early = self.leg(13, 30, pickup=DISNEY, dropoff=MCO)
        # Inside his whole-hour window (14-23), but leaving base 13:40 is before 14:15.
        before_base = self.leg(14, 30, pickup=DISNEY, dropoff=MCO)
        on_time = self.leg(15, 10, pickup=DISNEY, dropoff=MCO)   # leaves base 14:20
        self.confirm(EVENING)
        self.switch_on()
        got = self.assigned(self.preview(self.modal(self.d46)))
        self.assertNotEqual(got.get(early.id), 46)
        self.assertNotEqual(got.get(before_base.id), 46)
        self.assertEqual(got.get(on_time.id), 46)

    def test_morning_lead_refuses_disney_at_start(self):
        self.confirm(MORNING)
        self.switch_on()
        disney = self.leg(5, 0, pickup=DISNEY, dropoff=MCO)   # leaves base 04:10 < 04:35
        self.assertEqual(self.assigned(self.preview(self.modal(self.d46))), {})
        disney.delete()
        mco = self.leg(5, 0)                                  # leaves base 04:38
        self.assertEqual(self.assigned(self.preview(self.modal(self.d46))), {mco.id: 46})

    def test_modal_retyped_hours_keep_hours_and_skip_stub(self):
        self.leg(9, 0)
        self.confirm(("morning", time(4, 10), time(15, 30)))   # window (250, 970) = hours 4-17
        self.switch_on()
        seen = {}

        def spy(*args, **kwargs):
            seen["result"] = real(*args, **kwargs)
            return seen["result"]

        real = ap.run_assignment_pipeline
        with mock.patch.object(ap, "run_assignment_pipeline", side_effect=spy):
            self.preview({46: (4, 22)})
        window = seen["result"].capped_windows[46]
        self.assertEqual((window["start"], window["end"], window["source"]), (4, 22, "regular"))
        self.assertNotIn("start_min", window)
        self.assertEqual(window["max_hours"], fg.SPAN_HARD_HOURS_DEFAULT)   # not the stub's 14
        # Untouched hours carry the minute window.
        with mock.patch.object(ap, "run_assignment_pipeline", side_effect=spy):
            self.preview(self.modal(self.d46))
        window = seen["result"].capped_windows[46]
        self.assertEqual((window["start_min"], window["end_min"], window["max_span_min"]),
                         (250, 970, 720))

    def test_float_driver_takes_morning_and_evening_work_within_12h(self):
        self.confirm(FLOAT)
        self.switch_on()
        first = self.leg(5, 0)                                    # leaves base 04:38
        fits = self.leg(14, 30, pickup=DISNEY, dropoff=MCO)       # back 16:12: 11h 34m
        self.assertEqual(self.assigned(self.preview()), {first.id: 46, fits.id: 46})
        fits.delete()
        # Back at base 18:12 would be 13h 34m: the later one goes to someone else.
        late = self.leg(16, 30, pickup=DISNEY, dropoff=MCO)
        self.driver(47, "Other", default_flexible=False, default_start_hour=12,
                    default_end_hour=23)
        self.assertEqual(self.assigned(self.preview()), {first.id: 46, late.id: 47})

    def test_float_day_over_12h_leaves_a_leg_for_the_farm_list(self):
        self.confirm(FLOAT)
        self.switch_on()
        first = self.leg(5, 0)
        late = self.leg(16, 30, pickup=DISNEY, dropoff=MCO)
        got = self.assigned(self.preview())
        # Neither the span rescue nor the evict pass builds a 13h 34m day on him.
        self.assertEqual(len(got), 1, got)
        self.assertIn(next(iter(got)), (first.id, late.id))
        self.assertEqual(set(got.values()), {46})


class OtherEngineSitesTests(_EngineFixture):
    def test_conflict_advisor_tags_regular_not_stub(self):
        from dispatching.conflict_advisor import build_board_state
        self.leg(22, 30, pickup=DISNEY, dropoff=MCO, driver=self.d46)
        self.confirm(EVENING)
        board = build_board_state(DAY)
        self.assertEqual(board.window_sources[46], "stub")
        self.assertNotIn("start_min", board.windows[46])
        self.switch_on()
        board = build_board_state(DAY)
        self.assertEqual(board.window_sources[46], "configured")
        w = board.windows[46]
        self.assertEqual((w["start_min"], w["end_min"], w["kind"], w["max_span_min"]),
                         (855, 1575, "evening", 720))

    def test_day_roster_returns_regular_keys_when_on(self):
        from dispatching.day_planner import _day_roster
        self.confirm(EVENING)
        drivers, hours, flexible, max_hours, regular_keys = _day_roster(DAY)
        self.assertEqual([d.pk for d in drivers], [46])
        self.assertEqual((hours, regular_keys), ({46: (6, 23)}, {}))
        self.switch_on()
        drivers, hours, flexible, max_hours, regular_keys = _day_roster(DAY)
        self.assertEqual(hours, {46: (14, 23)})
        self.assertEqual(regular_keys, {46: {"start_min": 855, "end_min": 1575,
                                             "kind": "evening", "source": "regular",
                                             "max_span_min": 720}})

    def test_capacity_planner_suggestions_use_regular_window(self):
        late = self.leg(22, 30, pickup=DISNEY, dropoff=MCO)
        self.confirm(EVENING)

        def suggested():
            cache.delete(f"capacity_planner_{DAY.isoformat()}")
            resp = self.client.get(reverse("capacity_planner"), {"date": DAY.isoformat()})
            self.assertEqual(resp.status_code, 200)
            suggestion = resp.context["suggestion_map"].get(late.id)
            return suggestion.suggested_driver_id if suggestion else None

        self.assertIsNone(suggested())          # the stub's 20:00 clear-by
        self.switch_on()
        self.assertEqual(suggested(), 46)


class OverrunRegularDayTests(_EngineFixture):
    """S20 (§13 K9-K10, §6.5): the engine plans nothing more into a regular day already
    over 12h base to base; a dispatcher's own move onto it only warns."""

    def setUp(self):
        super().setUp()
        self.confirm(FLOAT)
        self.switch_on()
        # Built by hand: leave base 04:38 for the 05:00 MCO arrival; the 17:00 departure
        # clears 18:30 at MCO, back at base 18:42 — 14h 4m base to base.
        self.leg(5, 0, driver=self.d46)
        self.leg(17, 0, pickup=DISNEY, dropoff=MCO, driver=self.d46)
        # Fits inside the day: leaves base 09:10, back 12:05.
        self.hole = self.leg(10, 0, pickup=DISNEY, dropoff=DISNEY_2)

    def test_manual_move_onto_overrun_regular_day_only_warns(self):
        from dispatching.board_validation import revalidate_moves_against_db
        resp = self.client.get(reverse("check_driver_feasibility"),
                               {"leg_id": self.hole.id, "driver_id": 46})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["feasible"], body)
        self.assertIn("day already over 12h 0m base to base", body["warnings"])
        self.assertEqual(revalidate_moves_against_db([(self.hole.id, 46)], DAY), (True, ""))

    def test_engine_refuses_same_leg_on_overrun_regular_day(self):
        driver = Driver.objects.prefetch_related("weekly_schedule", "date_overrides").get(pk=46)
        eff = driver.get_effective_availability(DAY)
        keys = fg.regular_window_keys(eff)
        legs = list(Leg.objects.filter(pickup_date=DAY).select_related("driver", "reservation"))
        board = sch.build_driver_schedules(legs, [driver], DAY)
        suggestions = sch.suggest_assignments([self.hole], board, DAY, regular_keys={46: keys})
        self.assertFalse(any(s.leg_id == self.hole.id and s.suggested_driver_id == 46
                             for s in suggestions))
        window = fg.get_effective_window(46, configured={
            "start": eff["start_hour"], "end": eff["end_hour"], "max_hours": None,
            "flexible": False, **keys})
        result = check_feasibility(board[46], self.hole, DAY, driver_window=window)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason,
                         "Outside driver window: day already over 12h 0m base to base")
