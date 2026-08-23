"""Span Governor tests — duty-span caps + marginal effective-span pricing + coverage rescue.

Design record: docs/scheduler-automation/auto-assign-hour-balancing-design.md.
Pure/offline where possible (SimpleTestCase); the rescue tests build tiny in-memory boards.
"""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from dispatching import feasibility_guards as fg
from dispatching import scheduler as sch
from dispatching.scheduler import (
    DriverDaySchedule, ScheduleSlot, marginal_span_penalty, effective_span_hours,
    _span_gap_credit_minutes, _span_cost_points,
)

D = date(2026, 6, 9)


def _slot(leg_id, pickup_h, pickup_m=0, dur_min=60, pickup_loc="Disney Resort",
          dropoff_loc="MCO Terminal", trip_type="return"):
    pu = time(pickup_h, pickup_m)
    return ScheduleSlot(
        leg_id=leg_id, pickup_time=pu,
        pickup_location=pickup_loc, pickup_category=pickup_loc,
        dropoff_location=dropoff_loc, dropoff_category=dropoff_loc,
        trip_type=trip_type,
        estimated_end_time=datetime.combine(D, pu) + timedelta(minutes=dur_min),
        reservation_id=1, customer_name="", status="pending", has_flight=False,
    )


def _sched(driver_id, slots, name=None):
    return DriverDaySchedule(driver_id=driver_id, driver_name=name or f"d{driver_id}",
                             driver_type="inhouse", slots=slots)


# ════════════════════════════════════════════════════════════════════════════
# get_effective_window clamp
# ════════════════════════════════════════════════════════════════════════════
class EffectiveWindowCapTests(SimpleTestCase):
    def test_optimistic_stub_clamped_to_default(self):
        # David Encarancion stub max 24h -> the global default; start/end untouched.
        w = fg.get_effective_window(51)
        self.assertEqual(w["max_hours"], float(fg.SPAN_HARD_HOURS_DEFAULT))
        self.assertEqual(w["start"], fg.STUB_DRIVER_WINDOWS[51]["start"])
        self.assertEqual(w["end"], fg.STUB_DRIVER_WINDOWS[51]["end"])

    def test_default_is_founder_pick_15(self):
        # 2026-06-11 founder pick from the 18-day sweep: 15h default policy cap,
        # 17h absolute ceiling. Deliberate values — changing them is a decision.
        self.assertEqual(fg.SPAN_HARD_HOURS_DEFAULT, 15.0)
        self.assertEqual(fg.SPAN_ABS_CEILING_HOURS, 17.0)
        self.assertEqual(fg.SPAN_RESCUE_CEILING_HOURS, fg.SPAN_HARD_HOURS_DEFAULT)

    def test_tighter_stub_wins(self):
        # mesfin stub max 13h < default -> 13 stays.
        self.assertEqual(fg.get_effective_window(63)["max_hours"], 13.0)

    def test_typed_value_raises_above_default(self):
        # "A typed number means it": a dispatcher's 16h Max hrs on a long-leash
        # driver binds at 16 even though the default is lower (founder 2026-06-11:
        # "let's have the default fifteen ... I can always tweak that").
        w = fg.get_effective_window(51, configured={"start": 6, "end": 22,
                                                    "max_hours": 16.0, "flexible": False})
        self.assertEqual(w["max_hours"], 16.0)

    def test_typed_value_never_exceeds_absolute_ceiling(self):
        # The inhumane bound holds against any typed value.
        w = fg.get_effective_window(51, configured={"start": 6, "end": 22,
                                                    "max_hours": 18.0, "flexible": False})
        self.assertEqual(w["max_hours"], float(fg.SPAN_ABS_CEILING_HOURS))

    def test_stub_still_tightens_below_typed(self):
        # mesfin stub 13h: a typed 16 cannot raise observed reality — typed values
        # raise only the DEFAULT bound, never a tighter stub.
        w = fg.get_effective_window(63, configured={"max_hours": 16.0, "flexible": False})
        self.assertEqual(w["max_hours"], 13.0)

    def test_modal_cap_wins_when_tighter(self):
        w = fg.get_effective_window(51, configured={"start": 6, "end": 20,
                                                    "max_hours": 10.0, "flexible": False})
        self.assertEqual(w["max_hours"], 10.0)

    def test_enforce_cap_false_is_legacy(self):
        self.assertEqual(fg.get_effective_window(51, enforce_cap=False)["max_hours"], 24)
        self.assertIsNone(fg.get_effective_window(999999, enforce_cap=False))

    def test_flag_off_is_legacy(self):
        with patch.object(fg, "ENFORCE_SPAN_CAPS", False):
            self.assertEqual(fg.get_effective_window(51)["max_hours"], 24)
            self.assertIsNone(fg.get_effective_window(999999))

    def test_unknown_driver_synthetic_keeps_none_start_end(self):
        # start/end MUST stay None — a non-None end would newly enforce a clear-by on a
        # driver who today has no window at all.
        w = fg.get_effective_window(999999, configured={"flexible": True})
        self.assertEqual(w, {"start": None, "end": None,
                             "max_hours": float(fg.SPAN_HARD_HOURS_DEFAULT),
                             "flexible": True, "night_exempt": False})

    def test_stub_off_configured_clamped(self):
        # A typed 23h is intent, but intent stops at the absolute ceiling.
        with patch.object(fg, "USE_STUB_WINDOWS", False):
            w = fg.get_effective_window(1, configured={"start": 5, "end": 22,
                                                       "max_hours": 23.0, "flexible": False})
            self.assertEqual(w["max_hours"], float(fg.SPAN_ABS_CEILING_HOURS))
            self.assertEqual(w["start"], 5)

    def test_flexible_flag_still_honored_under_stub(self):
        w = fg.get_effective_window(51, configured={"flexible": True})
        self.assertTrue(w["flexible"])


# ════════════════════════════════════════════════════════════════════════════
# window_check frozen-driver delta gate
# ════════════════════════════════════════════════════════════════════════════
class WindowCheckDeltaGateTests(SimpleTestCase):
    W = {"start": None, "end": None, "max_hours": 17.0, "flexible": False}

    def test_over_cap_rejected(self):
        ok, reason = fg.window_check(self.W, time(5, 0), datetime(2026, 6, 9, 23, 0), 18.0,
                                     target_date=D, span_hours_before=12.0)
        self.assertFalse(ok)
        self.assertIn("max_hours", reason)

    def test_already_over_no_growth_allowed(self):
        # Day already 18h (pre-existing/manual board): a hole-fill that leaves the span
        # unchanged must NOT be frozen out by the cap.
        ok, _ = fg.window_check(self.W, time(12, 0), datetime(2026, 6, 9, 13, 0), 18.0,
                                target_date=D, span_hours_before=18.0)
        self.assertTrue(ok)

    def test_already_over_growth_rejected(self):
        ok, _ = fg.window_check(self.W, time(23, 0), datetime(2026, 6, 10, 0, 30), 19.5,
                                target_date=D, span_hours_before=18.0)
        self.assertFalse(ok)

    def test_legacy_no_before_rejects_total(self):
        ok, _ = fg.window_check(self.W, time(5, 0), datetime(2026, 6, 9, 23, 0), 18.0,
                                target_date=D)
        self.assertFalse(ok)

    def test_cap_applies_even_to_flexible(self):
        w = dict(self.W, flexible=True)
        ok, _ = fg.window_check(w, time(5, 0), datetime(2026, 6, 9, 23, 0), 18.0,
                                target_date=D, span_hours_before=12.0)
        self.assertFalse(ok)


# ════════════════════════════════════════════════════════════════════════════
# Effective span + marginal pricing
# ════════════════════════════════════════════════════════════════════════════
class NightLegFlexBlockTests(SimpleTestCase):
    """Founder rule 2026-06-10: a 00:00-02:59 pickup is night duty — Flexible means 'any
    time within a normal day', never the middle of the night. Explicit night windows
    (non-flexible, start covers the hour) still qualify."""

    def _win(self, flexible=True, start=None, end=None, max_hours=None):
        return {"start": start, "end": end, "max_hours": max_hours, "flexible": flexible}

    def _clear(self, h, m=0, dur_min=90):
        return datetime.combine(D, time(h, m)) + timedelta(minutes=dur_min)

    def test_flexible_blocked_for_night_pickup(self):
        ok, reason = fg.window_check(self._win(flexible=True), time(0, 30),
                                     self._clear(0, 30), 2.0, target_date=D)
        self.assertFalse(ok)
        self.assertIn("night", reason)

    def test_flexible_allowed_at_boundary(self):
        # 03:00 is a legitimate early-morning day start (founder boards: Michael Olmo 03:00).
        ok, _ = fg.window_check(self._win(flexible=True), time(3, 0),
                                self._clear(3, 0), 2.0, target_date=D)
        self.assertTrue(ok)

    def test_explicit_night_window_allowed(self):
        # A deliberate night shift (non-flexible, start 0) takes the 00:30 arrival.
        ok, _ = fg.window_check(self._win(flexible=False, start=0, end=8), time(0, 30),
                                self._clear(0, 30), 2.0, target_date=D)
        self.assertTrue(ok)

    def test_non_flexible_day_window_still_blocked_by_start(self):
        ok, reason = fg.window_check(self._win(flexible=False, start=9, end=23), time(0, 30),
                                     self._clear(0, 30), 2.0, target_date=D)
        self.assertFalse(ok)
        self.assertIn("before start", reason)

    def test_explicit_start_beats_flexible_flag(self):
        # Builder/advisor escape: a dispatcher typing From=00:00 (or an accepted advisor
        # night card) is explicit — the flexible DB flag must not override it.
        ok, _ = fg.window_check(self._win(flexible=True, start=0, end=8), time(0, 30),
                                self._clear(0, 30), 2.0, target_date=D)
        self.assertTrue(ok)

    def test_night_exempt_window_bypasses_rule(self):
        # Manual-sovereign escape: execute_swap revalidation windows carry night_exempt
        # so a dispatcher's intentional move (or a pre-existing night leg re-checked by
        # the all-legs revalidation loop) is never hard-blocked.
        w = self._win(flexible=True)
        w["night_exempt"] = True
        ok, _ = fg.window_check(w, time(0, 30), self._clear(0, 30), 2.0, target_date=D)
        self.assertTrue(ok)

    def test_get_effective_window_marks_manual_sovereign(self):
        # enforce_cap=False (manual swap revalidation / analytics) -> night_exempt=True;
        # the default auto-assign path (enforce_cap=True) -> night_exempt=False.
        stub_id = next(iter(fg.STUB_DRIVER_WINDOWS))
        manual = fg.get_effective_window(stub_id, configured={"flexible": True},
                                         enforce_cap=False)
        auto = fg.get_effective_window(stub_id, configured={"flexible": True})
        self.assertTrue(manual["night_exempt"])
        self.assertFalse(auto["night_exempt"])

    def test_flag_off_is_legacy(self):
        with patch.object(fg, "NIGHT_LEG_FLEX_BLOCK", False):
            ok, _ = fg.window_check(self._win(flexible=True), time(0, 30),
                                    self._clear(0, 30), 2.0, target_date=D)
        self.assertTrue(ok)


class EffectiveSpanPricingTests(SimpleTestCase):
    def test_gap_credit_only_for_real_breaks(self):
        slots = [_slot(1, 5, dur_min=60), _slot(2, 7, dur_min=60)]   # 60-min gap: no credit
        self.assertEqual(_span_gap_credit_minutes(slots, D), 0.0)
        slots = [_slot(1, 5, dur_min=60), _slot(2, 9, dur_min=60)]   # 180-min gap: credited
        self.assertEqual(_span_gap_credit_minutes(slots, D), 180.0)

    def test_gap_credit_capped(self):
        slots = [_slot(1, 4, dur_min=60), _slot(2, 13, dur_min=60)]  # 8h hole -> capped at 300
        self.assertEqual(_span_gap_credit_minutes(slots, D), 300.0)

    def test_founder_split_day_not_over_target(self):
        # 03:30 start, 4.5h hole, 20:00 end: 16.5h raw but ~12h effective -> under 13.5 target.
        slots = [_slot(1, 3, 30, dur_min=120), _slot(2, 10, 0, dur_min=120),
                 _slot(3, 16, 30, dur_min=120), _slot(4, 19, 0, dur_min=60)]
        raw, eff = effective_span_hours(slots, D)
        self.assertAlmostEqual(raw, 16.5, places=1)
        self.assertLessEqual(eff, 13.5)

    def test_marginal_price_prefers_late_driver_for_late_leg(self):
        # Early driver 04:00-12:00 (compact, no creditable break); late driver 14:00-18:00.
        # A 21:00 leg must cost the early driver (8h day -> 18.5h) far more than the late
        # one (4h -> 8.5h: free band).
        early = [_slot(1, 4, dur_min=120), _slot(2, 7, dur_min=120), _slot(3, 10, dur_min=120)]
        late = [_slot(4, 14, dur_min=120), _slot(5, 16, dur_min=120)]
        pu = datetime.combine(D, time(21, 0)); end = pu + timedelta(minutes=90)
        p_early = marginal_span_penalty(early, D, pu, end)
        p_late = marginal_span_penalty(late, D, pu, end)
        self.assertEqual(p_late, 0)
        self.assertGreater(p_early, 200)   # steep band: > any single scoring bonus

    def test_empty_schedule_costs_nothing(self):
        pu = datetime.combine(D, time(22, 0)); end = pu + timedelta(minutes=90)
        self.assertEqual(marginal_span_penalty([], D, pu, end), 0)

    def test_insert_inside_day_is_free(self):
        slots = [_slot(1, 6, dur_min=60), _slot(2, 20, dur_min=60)]
        pu = datetime.combine(D, time(12, 0)); end = pu + timedelta(minutes=60)
        self.assertEqual(marginal_span_penalty(slots, D, pu, end), 0)

    def test_no_credit_for_hole_minted_by_the_insert(self):
        # Compact 05:00-09:00 day; a 16:00 leg creates a 7h hole. The credit must come from
        # the PRE-insert schedule (no >=2h hole exists), so the full stretch is charged.
        slots = [_slot(1, 5, dur_min=120), _slot(2, 8, dur_min=60)]
        pu = datetime.combine(D, time(16, 0)); end = pu + timedelta(minutes=60)
        p = marginal_span_penalty(slots, D, pu, end)
        # raw 4h -> 12h, no credit: cost(12)-cost(4) = 0 under free band... stretch further:
        pu2 = datetime.combine(D, time(20, 0)); end2 = pu2 + timedelta(minutes=60)
        p2 = marginal_span_penalty(slots, D, pu2, end2)   # raw -> 16h effective
        self.assertGreaterEqual(p, 0)
        self.assertGreater(p2, 300)  # 12->13.5 soft + 13.5->16 steep, no gap credit

    def test_strictly_greater_target_no_steep_at_exact_target(self):
        self.assertEqual(_span_cost_points(13.5), 25 * 1.5)  # soft band only, no steep


# ════════════════════════════════════════════════════════════════════════════
# Coverage rescue pass
# ════════════════════════════════════════════════════════════════════════════
class _FakeLeg(SimpleNamespace):
    def get_trip_type(self):
        return getattr(self, "trip_type", "return")


def _leg(leg_id, pickup_h, pickup_m=0, vtype="suv", revenue=100, driver_id=None):
    return _FakeLeg(
        id=leg_id, pickup_time=time(pickup_h, pickup_m),
        pickup_location="Disney Resort", dropoff_location="MCO Terminal",
        effective_vehicle_type=vtype, revenue_share=revenue,
        driver=None, driver_id=driver_id, reservation_id=1, status="pending",
        flight_information=None, trip_type="return",
    )


class RescuePassTests(TestCase):
    """The rescue board is built via build_driver_schedules, which needs real-ish legs;
    we monkeypatch build_driver_schedules to construct boards from our fake legs directly.
    TestCase (not SimpleTestCase): check_feasibility/estimate_job_end_time read
    SchedulerSettings + route metrics (empty test DB -> category-table fallbacks)."""

    def setUp(self):
        self.drivers = [SimpleNamespace(id=7, __str__=lambda s: "Seven"),
                        SimpleNamespace(id=8, __str__=lambda s: "Eight")]
        self.drivers_by_id = {d.id: d for d in self.drivers}
        self.dvtypes = {7: "suv", 8: "suv"}

        def fake_build(ih_legs, drivers, target_date):
            boards = {d.id: _sched(d.id, []) for d in drivers}
            for l in ih_legs:
                if l.driver_id in boards:
                    boards[l.driver_id].slots.append(
                        _slot(l.id, l.pickup_time.hour, l.pickup_time.minute, dur_min=90))
            for b in boards.values():
                b.slots.sort(key=lambda s: s.pickup_time)
            return boards
        self._patch = patch.object(sch, "build_driver_schedules", side_effect=fake_build)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _windows(self, cap7=10.0, cap8=10.0):
        return {7: {"start": None, "end": None, "max_hours": cap7, "flexible": False},
                8: {"start": None, "end": None, "max_hours": cap8, "flexible": False}}

    def test_span_blocked_leg_rescued_with_warning(self):
        # Driver 7 works 05:00-13:30 (8.5h); an 18:00 leg stretches him to ~14.5h:
        # over his 10h cap, under the rescue ceiling (15h default). Driver 8 is
        # windowed out by modal hours. Rescue must lift 7's (non-strict) cap.
        base = {1: 7}
        legs_by_id = {1: _leg(1, 5), 2: _leg(2, 12), 99: _leg(99, 18)}
        legs_by_id[2].driver_id = 7   # pre-existing
        base = {1: 7, 2: 7}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            dict(base), [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(), driver_hours={7: (4, 23), 8: (4, 10)},
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertIn(99, rescued)
        self.assertEqual(fa[99], 7)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["kind"], "rescued")
        self.assertEqual(warnings[0]["driver_id"], 7)

    def test_strict_cap_never_lifted(self):
        # 18:00 leg = ~14.5h, UNDER the rescue ceiling, so the strict gate (not the
        # ceiling) is what blocks: a typed Max hrs is authoritative.
        legs_by_id = {1: _leg(1, 5, driver_id=7), 2: _leg(2, 12, driver_id=7), 99: _leg(99, 18)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7, 2: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(), driver_hours={7: (4, 23), 8: (4, 10)},
            flexible_drivers=None, strict_cap_driver_ids={7}, locked_leg_ids=set())
        self.assertEqual(rescued, [])
        self.assertNotIn(99, fa)
        self.assertEqual(warnings[0]["kind"], "strict_blocked")
        self.assertEqual(warnings[0]["driver_id"], 7)

    def test_tier_mismatch_not_rescued(self):
        # A van leg must never be rescued onto an suv driver (check_feasibility doesn't
        # check tier — the rescue must).
        legs_by_id = {1: _leg(1, 5, driver_id=7), 99: _leg(99, 19, vtype="Van(14 Pax)")}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(), driver_hours=None,
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertEqual(rescued, [])
        self.assertNotIn(99, fa)

    def test_modal_window_respected(self):
        # Both drivers' modal End is 14 -> a 19:00 leg has no candidates at all.
        legs_by_id = {1: _leg(1, 5, driver_id=7), 99: _leg(99, 19)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(), driver_hours={7: (4, 14), 8: (4, 14)},
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertEqual(rescued, [])
        self.assertEqual(warnings, [])

    def test_feasible_under_cap_assigned_without_warning(self):
        # Driver 8 is free all day: the 19:00 leg fits under his cap -> normal assign, no badge.
        legs_by_id = {1: _leg(1, 5, driver_id=7), 99: _leg(99, 19)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(), driver_hours={7: (4, 23), 8: (4, 23)},
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertEqual(fa[99], 8)
        self.assertEqual(warnings, [])

    def test_rescue_never_exceeds_absolute_ceiling(self):
        # Driver 7 works 05:00-06:30; a 23:00 leg would make a ~19.5h day. The lift stops
        # at SPAN_RESCUE_CEILING_HOURS (= the 15h policy default): the leg must stay
        # residual and FARM rather than build an inhumane day (founder rule 2026-06-10)
        # — and the farm must be LOUD: a ceiling_blocked warning, never a silent
        # disappearance. Driver 8 windowed out.
        legs_by_id = {1: _leg(1, 5, driver_id=7), 99: _leg(99, 23)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(), driver_hours={7: (4, 23), 8: (4, 10)},
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertEqual(rescued, [])
        self.assertNotIn(99, fa)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["kind"], "ceiling_blocked")
        self.assertEqual(warnings[0]["driver_id"], 7)
        self.assertEqual(warnings[0]["cap_hours"], float(fg.SPAN_RESCUE_CEILING_HOURS))

    def test_cap_already_at_ceiling_still_loud(self):
        # A cap equal to the ceiling has nothing to lift — the leg stays residual but the
        # farm is still explained with a ceiling_blocked warning.
        legs_by_id = {1: _leg(1, 5, driver_id=7), 99: _leg(99, 23)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(cap7=float(fg.SPAN_RESCUE_CEILING_HOURS)),
            driver_hours={7: (4, 23), 8: (4, 10)},
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertEqual(rescued, [])
        self.assertNotIn(99, fa)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["kind"], "ceiling_blocked")

    def test_rescue_below_ceiling_still_works(self):
        # 05:00-13:30 + 18:00 leg = ~14.5h: above the 10h cap, below the 15h ceiling ->
        # rescued with the red badge exactly as before the ceiling existed.
        legs_by_id = {1: _leg(1, 5, driver_id=7), 2: _leg(2, 12, driver_id=7), 99: _leg(99, 18)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7, 2: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(), driver_hours={7: (4, 23), 8: (4, 10)},
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertEqual(fa.get(99), 7)
        self.assertEqual(warnings[0]["kind"], "rescued")

    def test_turnaround_blocked_leg_not_rescued(self):
        # A leg overlapping driver 7's existing job is infeasible even uncapped -> no rescue
        # (driver 8 is windowed out by his modal hours, so 7 is the only candidate).
        legs_by_id = {1: _leg(1, 19, driver_id=7), 99: _leg(99, 19, 30)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(cap7=1.0), driver_hours={7: (4, 23), 8: (4, 10)},
            flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
        self.assertEqual(rescued, [])
        self.assertEqual(warnings, [])

    def test_typed_cap_at_or_above_ceiling_reports_strict_not_ceiling(self):
        # Typed 16h (>= the 15h rescue ceiling): nothing to lift, and the warning must
        # name HIS typed cap (strict_blocked @ 16), never claim the policy ceiling —
        # the tooltip promises typed values up to the 17h absolute ceiling work.
        legs_by_id = {1: _leg(1, 5, driver_id=7), 2: _leg(2, 12, driver_id=7), 99: _leg(99, 21)}
        fa, rescued, warnings = sch.rescue_span_blocked_residuals(
            {1: 7, 2: 7}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
            self._windows(cap7=16.0), driver_hours={7: (4, 23), 8: (4, 10)},
            flexible_drivers=None, strict_cap_driver_ids={7}, locked_leg_ids=set())
        self.assertEqual(rescued, [])
        self.assertNotIn(99, fa)
        self.assertEqual(warnings[0]["kind"], "strict_blocked")
        self.assertEqual(warnings[0]["cap_hours"], 16.0)

    def test_deterministic(self):
        # 18:00 leg (~14.5h) keeps this on the RESCUED branch it was written to pin
        # (a 19:00 leg would now exercise ceiling-block determinism instead).
        legs_by_id = {1: _leg(1, 5, driver_id=7), 2: _leg(2, 12, driver_id=7), 99: _leg(99, 18)}
        runs = []
        for _ in range(2):
            fa, rescued, warnings = sch.rescue_span_blocked_residuals(
                {1: 7, 2: 7}, [99], dict(legs_by_id), self.drivers, self.drivers_by_id, D,
                self.dvtypes, self._windows(), driver_hours={7: (4, 23), 8: (4, 10)},
                flexible_drivers=None, strict_cap_driver_ids=set(), locked_leg_ids=set())
            runs.append((dict(fa), list(rescued), [w["kind"] for w in warnings]))
        self.assertEqual(runs[0], runs[1])

    def test_flag_off_noop(self):
        legs_by_id = {99: _leg(99, 19)}
        with patch.object(sch, "SPAN_COVERAGE_RESCUE", False):
            fa, rescued, warnings = sch.rescue_span_blocked_residuals(
                {}, [99], legs_by_id, self.drivers, self.drivers_by_id, D, self.dvtypes,
                self._windows())
        self.assertEqual((fa, rescued, warnings), ({}, [], []))


# ════════════════════════════════════════════════════════════════════════════
# Span-trim relocation pass
# ════════════════════════════════════════════════════════════════════════════
class TrimPassTests(TestCase):
    """Same fake-board pattern as RescuePassTests. Donor 7 has a 04:00-20:30 day (16.5h raw,
    compact, no break credit); receiver 8 has a short midday day with evening room."""

    def setUp(self):
        self.drivers = [SimpleNamespace(id=7, __str__=lambda s: "Seven"),
                        SimpleNamespace(id=8, __str__=lambda s: "Eight")]
        self.drivers_by_id = {d.id: d for d in self.drivers}
        self.dvtypes = {7: "suv", 8: "suv"}

        def fake_build(ih_legs, drivers, target_date):
            boards = {d.id: _sched(d.id, []) for d in drivers}
            for l in ih_legs:
                if l.driver_id in boards:
                    boards[l.driver_id].slots.append(
                        _slot(l.id, l.pickup_time.hour, l.pickup_time.minute, dur_min=90))
            for b in boards.values():
                b.slots.sort(key=lambda s: s.pickup_time)
            return boards
        self._patch = patch.object(sch, "build_driver_schedules", side_effect=fake_build)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _long_day(self):
        """Donor 7: legs every ~2h, 04:00..19:00 (last clears 20:30) -> 16.5h raw."""
        legs = {}
        for i, h in enumerate([4, 6, 8, 10, 12, 14, 16, 19], start=1):
            legs[i] = _leg(i, h)
        fa = {i: 7 for i in legs}
        # Receiver 8: one midday leg, plenty of evening room under 13.5h.
        legs[50] = _leg(50, 11)
        fa[50] = 8
        return fa, legs

    def _windows(self):
        return {7: {"start": None, "end": None, "max_hours": 17.0, "flexible": False},
                8: {"start": None, "end": None, "max_hours": 17.0, "flexible": False}}

    def _run(self, fa, legs, **kw):
        defaults = dict(locked_leg_ids=set(), driver_hours={7: (0, 23), 8: (0, 23)},
                        flexible_drivers=None, capped_windows=self._windows())
        defaults.update(kw)
        return sch.trim_spans_via_relocation(
            fa, legs, self.drivers, self.drivers_by_id, D, self.dvtypes, **defaults)

    def test_boundary_leg_moves_to_short_driver(self):
        fa, legs = self._long_day()
        keys_before = set(fa.keys())
        fa, moves = self._run(fa, legs)
        self.assertGreaterEqual(len(moves), 1)
        self.assertEqual(set(fa.keys()), keys_before)          # coverage invariant
        moved_ids = {m["leg_id"] for m in moves}
        self.assertTrue(moved_ids <= {1, 8})                   # only first/last legs ever move
        self.assertTrue(all(m["from"] == 7 and m["to"] == 8 for m in moves))

    def test_locked_boundary_legs_never_move(self):
        fa, legs = self._long_day()
        fa, moves = self._run(fa, legs, locked_leg_ids={1, 8})
        self.assertEqual(moves, [])

    def test_receiver_never_minted_long(self):
        # Receiver 8 already has a 04:00 leg: taking the 19:00 tail would put him over the
        # 15h raw gate (04:00 -> >=19:00 even with the real end-time estimator) -> no move.
        fa, legs = self._long_day()
        legs[51] = _leg(51, 4)
        fa[51] = 8
        fa, moves = self._run(fa, legs)
        for m in moves:
            self.assertNotEqual(m["leg_id"], 8)  # the 19:00-side tail can't go to 8 anymore

    def test_modal_window_respected(self):
        fa, legs = self._long_day()
        fa, moves = self._run(fa, legs, driver_hours={7: (0, 23), 8: (10, 14)})
        # 8's hard window 10-14: neither the 04:00 nor the 19:00 boundary leg fits him.
        self.assertEqual(moves, [])

    def test_deterministic(self):
        runs = []
        for _ in range(2):
            fa, legs = self._long_day()
            fa, moves = self._run(fa, legs)
            runs.append((dict(fa), [tuple(sorted(m.items())) for m in moves]))
        self.assertEqual(runs[0], runs[1])

    def test_flag_off_noop(self):
        fa, legs = self._long_day()
        with patch.object(sch, "AUTO_SPAN_TRIM_PASS", False):
            fa2, moves = self._run(dict(fa), legs)
        self.assertEqual(moves, [])
        self.assertEqual(fa2, fa)


class SharerConflictTests(TestCase):
    """Shared-car occupancy gate: one physical unit, two drivers - an insert for one must
    never overlap the partner's jobs (the founder's two-conflicting-jobs-on-#001 bug)."""

    def _board(self):
        # Partner (id 7) holds a 14:30 job that clears ~16:00.
        return {7: _sched(7, [_slot(1, 14, 30, dur_min=90)]), 8: _sched(8, [])}

    def _mk_leg(self, h, m=0):
        return _FakeLeg(id=99, pickup_time=time(h, m), pickup_location="Disney Resort",
                        dropoff_location="MCO Terminal", effective_vehicle_type="suv",
                        revenue_share=100, driver=None, driver_id=None, reservation_id=1,
                        status="pending", flight_information=None, trip_type="return")

    def test_overlapping_job_blocked_for_partner(self):
        # 15:15 pickup while the partner's 14:30 job is still running -> conflict.
        self.assertTrue(sch.sharers_conflict(
            self._mk_leg(15, 15), 8, {8: {7}}, self._board(), D))

    def test_pad_blocks_tight_handoff(self):
        # Partner clears ~16:00; a 16:15 pickup is inside the handoff pad -> conflict.
        self.assertTrue(sch.sharers_conflict(
            self._mk_leg(16, 15), 8, {8: {7}}, self._board(), D))

    def test_pad_default_is_empirical_120(self):
        # Scheduling redesign Build 1c: the flat 60-min constant sat near the 9th
        # percentile of measured pickup-to-pickup handoff gaps, so the pad now lives
        # on SchedulerSettings.vehicle_share_pad_min, default 120 (the empirical
        # anchor). Pin the default AND the behavior boundary it creates: partner
        # clears 16:00, so a 17:05 pickup (clean under the old 60) now conflicts,
        # while an 18:05 pickup (just past clear + 120) is clean.
        from dispatching.models import SchedulerSettings
        SchedulerSettings.clear_cache()
        self.addCleanup(SchedulerSettings.clear_cache)
        self.assertEqual(
            SchedulerSettings.get_settings().vehicle_share_pad_min, 120)
        self.assertTrue(sch.sharers_conflict(
            self._mk_leg(17, 5), 8, {8: {7}}, self._board(), D))
        self.assertFalse(sch.sharers_conflict(
            self._mk_leg(18, 5), 8, {8: {7}}, self._board(), D))

    def test_pad_is_live_editable(self):
        # The founder can tune the pad without a deploy: drop it to 60 and the
        # old boundary comes back (16:45 conflicts, 17:05 is clean again).
        from dispatching.models import SchedulerSettings
        SchedulerSettings.clear_cache()
        self.addCleanup(SchedulerSettings.clear_cache)
        cfg = SchedulerSettings.get_settings()
        cfg.vehicle_share_pad_min = 60
        cfg.save()
        SchedulerSettings.clear_cache()
        self.assertTrue(sch.sharers_conflict(
            self._mk_leg(16, 45), 8, {8: {7}}, self._board(), D))
        self.assertFalse(sch.sharers_conflict(
            self._mk_leg(17, 5), 8, {8: {7}}, self._board(), D))

    def test_explicit_pad_min_still_wins(self):
        # A caller-passed pad bypasses the setting (the engine's own passes rely
        # on this for what-if scoring).
        self.assertTrue(sch.sharers_conflict(
            self._mk_leg(16, 45), 8, {8: {7}}, self._board(), D, pad_min=60))
        self.assertFalse(sch.sharers_conflict(
            self._mk_leg(17, 5), 8, {8: {7}}, self._board(), D, pad_min=60))

    def test_clean_evening_job_allowed(self):
        # 18:30 pickup, well after the partner clears + pad -> fine.
        self.assertFalse(sch.sharers_conflict(
            self._mk_leg(18, 30), 8, {8: {7}}, self._board(), D))

    def test_non_sharer_unaffected(self):
        self.assertFalse(sch.sharers_conflict(
            self._mk_leg(15, 15), 8, None, self._board(), D))
        self.assertFalse(sch.sharers_conflict(
            self._mk_leg(15, 15), 9, {8: {7}}, self._board(), D))
