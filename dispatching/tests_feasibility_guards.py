"""Unit tests for dispatching.feasibility_guards (pure functions, no DB).

Guard A (capacity_fit) was removed — booking-time validation enforces capacity — so only
Guard B (turnaround) and Guard C (window) logic is tested here.
"""
from datetime import datetime, date, time
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase

from dispatching import feasibility_guards as fg
from dispatching.scheduler import check_feasibility
from dispatching.tests_founder_brain import _leg as _fleg, _slot as _fslot, _sched as _fsched, D as _FB_D


class TurnaroundTests(SimpleTestCase):
    def test_deplaning_grace_is_ten(self):
        # Founder set the deplaning window to 10 min (2026-06-12): a 10:30 flight => latest
        # curbside 10:40.
        self.assertEqual(fg.DEPLANING_GRACE_MIN, 10)

    def test_non_arrival_full_drive_only(self):
        # anything -> non-arrival (incl. Port): full drive, pad now 0 (live monitoring)
        self.assertEqual(fg.required_turnaround(30, next_is_airport_arrival=False, same_terminal=False), 30)

    def test_airport_arrival_same_terminal(self):
        # same-terminal arrival (driver ALREADY at the airport): -grace, may go negative — the
        # pickup can be ~grace min before he clears the prev job (pax still deplaning).
        self.assertEqual(fg.required_turnaround(45, next_is_airport_arrival=True, same_terminal=True), -10)

    def test_airport_arrival_from_resort_no_grace(self):
        # resort -> airport arrival: he must DRIVE in, so the FULL reposition drive is required
        # and the deplaning grace is NOT credited (the fix for the Roberto / runer overlaps).
        self.assertEqual(fg.required_turnaround(45, next_is_airport_arrival=True, same_terminal=False), 45)

    def test_repositioning_arrival_never_goes_negative(self):
        # A short drive in from elsewhere still requires the full drive — no negative credit
        # (only a driver standing at the same terminal earns the deplaning discount).
        self.assertEqual(fg.required_turnaround(10, next_is_airport_arrival=True, same_terminal=False), 10)

    def test_safety_pad_default_is_zero(self):
        self.assertEqual(fg.SAFETY_PAD_MIN, 0)

    def test_custom_grace_honored_same_terminal(self):
        # explicit grace override applies on the same-terminal (at-airport) path: -grace + pad
        self.assertEqual(
            fg.required_turnaround(60, next_is_airport_arrival=True, same_terminal=True,
                                   deplaning_grace=30, safety_pad=5),
            -30 + 5)

    def test_custom_pad_honored_repositioning(self):
        # repositioning in: full drive + pad, grace ignored regardless of its value
        self.assertEqual(
            fg.required_turnaround(60, next_is_airport_arrival=True, same_terminal=False,
                                   deplaning_grace=30, safety_pad=5),
            60 + 5)

    def test_is_airport_arrival(self):
        self.assertTrue(fg.is_airport_arrival("arrival", "MCO Terminal"))
        self.assertFalse(fg.is_airport_arrival("return", "MCO Terminal"))
        self.assertFalse(fg.is_airport_arrival("arrival", "Disney"))


class WindowCheckTests(SimpleTestCase):
    def W(self, start=6, end=17, max_hours=None, flexible=False):
        return {"start": start, "end": end, "max_hours": max_hours, "flexible": flexible}

    def test_none_window_skips(self):
        ok, _ = fg.window_check(None, time(3, 0), datetime(2026, 5, 1, 23, 0), 20)
        self.assertTrue(ok)

    def test_before_start_fails(self):
        ok, reason = fg.window_check(self.W(start=6), time(5, 0), datetime(2026, 5, 1, 8, 0), 3)
        self.assertFalse(ok)
        self.assertIn("before start", reason)

    def test_flexible_bypasses_start(self):
        ok, _ = fg.window_check(self.W(start=6, flexible=True), time(4, 0), datetime(2026, 5, 1, 8, 0), 4)
        self.assertTrue(ok)

    def test_clear_by_violation(self):
        # clears 18:30, end 17 -> CLEAR_BY fail
        ok, reason = fg.window_check(self.W(end=17), time(15, 0), datetime(2026, 5, 1, 18, 30), 4)
        self.assertFalse(ok)
        self.assertIn("clear-by", reason)

    def test_clear_exactly_at_end_ok(self):
        ok, _ = fg.window_check(self.W(end=17), time(15, 0), datetime(2026, 5, 1, 17, 0), 2)
        self.assertTrue(ok)

    def test_flexible_bypasses_clear_by_by_default(self):
        # FLEXIBLE_RESPECTS_CLEAR_BY default False: a flexible driver works/finishes anytime,
        # so clearing past the (nominal) end is allowed.
        ok, _ = fg.window_check(self.W(end=17, flexible=True), time(16, 0), datetime(2026, 5, 1, 18, 30), 3)
        self.assertTrue(ok)

    def test_flexible_still_bound_when_frcb_enabled(self):
        # Explicit override re-imposes the clear-by on a flexible driver.
        ok, reason = fg.window_check(self.W(end=17, flexible=True), time(16, 0), datetime(2026, 5, 1, 18, 30), 3,
                                     flexible_respects_clear_by=True)
        self.assertFalse(ok)
        self.assertIn("clear-by", reason)

    def test_last_pickup_mode(self):
        ok, reason = fg.window_check(self.W(end=17), time(18, 0), datetime(2026, 5, 1, 19, 0), 2, mode="LAST_PICKUP")
        self.assertFalse(ok)
        self.assertIn("last-pickup", reason)

    def test_max_hours_exceeded(self):
        ok, reason = fg.window_check(self.W(end=23, max_hours=10), time(6, 0), datetime(2026, 5, 1, 20, 0), 14)
        self.assertFalse(ok)
        self.assertIn("max_hours", reason)

    def test_max_hours_ok(self):
        ok, _ = fg.window_check(self.W(end=23, max_hours=14), time(6, 0), datetime(2026, 5, 1, 18, 0), 12)
        self.assertTrue(ok)

    # ── absolute clear-by (target_date) path, incl. after-midnight (H3) ──
    def test_clear_by_with_date_violation(self):
        ok, reason = fg.window_check(self.W(end=17), time(15, 0), datetime(2026, 5, 1, 18, 30), 4,
                                     target_date=date(2026, 5, 1))
        self.assertFalse(ok)
        self.assertIn("clear-by", reason)

    def test_clear_by_with_date_exactly_ok(self):
        ok, _ = fg.window_check(self.W(end=17), time(15, 0), datetime(2026, 5, 1, 17, 0), 2,
                                target_date=date(2026, 5, 1))
        self.assertTrue(ok)

    def test_clear_after_midnight_fails(self):
        # 22:30 pickup clearing 00:30 the NEXT day must violate a 23:00 clear-by,
        # not evade it via a bare-hour (0 < 23) comparison.
        ok, reason = fg.window_check(self.W(end=23), time(22, 30), datetime(2026, 5, 2, 0, 30), 2,
                                     target_date=date(2026, 5, 1))
        self.assertFalse(ok)
        self.assertIn("clear-by", reason)


class EffectiveWindowTests(SimpleTestCase):
    def test_stub_mode_returns_stub(self):
        self.assertTrue(fg.USE_STUB_WINDOWS)
        w = fg.get_effective_window(46)  # Yovanny Suarez stub
        self.assertIsNotNone(w)
        self.assertEqual(w["flexible"], False)
        self.assertIn("end", w)

    def test_unknown_driver_returns_none(self):
        # Legacy semantics (no Span Governor): unknown driver => no window guard at all.
        self.assertIsNone(fg.get_effective_window(999999, enforce_cap=False))

    def test_unknown_driver_capped_synthetic(self):
        # Span Governor: an unknown driver gets a cap-only synthetic window whose start/end
        # stay None (a non-None end would NEWLY enforce a clear-by he never had).
        w = fg.get_effective_window(999999)
        self.assertIsNotNone(w)
        self.assertIsNone(w["start"])
        self.assertIsNone(w["end"])
        self.assertEqual(w["max_hours"], fg.SPAN_HARD_HOURS_DEFAULT)

    def test_stub_false_uses_configured(self):
        # H2: flipping USE_STUB_WINDOWS=False must switch to the configured window,
        # NOT silently disable the guard (return None).
        configured = {"start": 6, "end": 17, "max_hours": 12.0, "flexible": False}
        with patch.object(fg, "USE_STUB_WINDOWS", False):
            # max_hours 12 < the global default cap, so the capped window equals configured
            # (plus the night_exempt marker: False on the auto path, True on the
            # manual-sovereign enforce_cap=False path).
            self.assertEqual(fg.get_effective_window(46, configured=configured),
                             dict(configured, night_exempt=False))
            self.assertEqual(fg.get_effective_window(46, configured=configured, enforce_cap=False),
                             dict(configured, night_exempt=True))
            self.assertIsNone(fg.get_effective_window(46, configured=None, enforce_cap=False))


class ArrivalChainOverlapTests(TestCase):
    """Regression for the 2026-06-12 Roberto / runer overlaps: an arrival that FOLLOWS a job at
    a different location must require the full reposition drive (no deplaning grace for a driver
    who is still 25-40 min away). The legitimate same-airport turn stays feasible."""

    def test_runer_resort_to_airport_arrival_rejected(self):
        # 7:25 MCO->Pop Century arrival (chain-clears ~8:40 static), then 8:55 MCO->Pop Century
        # arrival. Disney->MCO reposition 30, NO grace => earliest 9:10 > 8:55 => infeasible.
        # (Under the old -15 grace this slipped through at buffer 0.)
        prev = _fleg(1, 7, 25, vtype="towncar", trip="arrival",
                     pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        nxt = _fleg(2, 8, 55, vtype="mini_van", trip="arrival",
                    pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        res = check_feasibility(_fsched(6, [_fslot(prev)]), nxt, _FB_D)
        self.assertFalse(res.feasible)

    def test_roberto_resort_to_airport_arrival_rejected(self):
        # 9:00 MCO->Hard Rock (Universal) arrival (chain-clears ~10:10 static), then 10:27
        # MCO->Art of Animation arrival. Universal->MCO 25, NO grace => earliest 10:35 > 10:27.
        prev = _fleg(1, 9, 0, vtype="van", trip="arrival",
                     pickup_loc="MCO Terminal", dropoff_loc="Universal Resort")
        nxt = _fleg(2, 10, 27, vtype="suv", trip="arrival",
                    pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        res = check_feasibility(_fsched(7, [_fslot(prev)]), nxt, _FB_D)
        self.assertFalse(res.feasible)

    def test_at_airport_turn_still_feasible(self):
        # Driver drops a return AT MCO (chain-clears ~1:35), then grabs an MCO arrival at 1:34 —
        # already at the airport (same_terminal), pax deplaning => -10 grace keeps it feasible.
        prev = _fleg(1, 13, 5, vtype="suv", trip="return",
                     pickup_loc="Disney Resort", dropoff_loc="MCO Terminal")
        nxt = _fleg(2, 13, 34, vtype="suv", trip="arrival",
                    pickup_loc="MCO Terminal", dropoff_loc="Disney Resort")
        res = check_feasibility(_fsched(8, [_fslot(prev)]), nxt, _FB_D)
        self.assertTrue(res.feasible)


class GuardAPrimeVehicleCapTests(TestCase):
    """Guard A' — opt-in per-unit scheduling cap (FleetVehicle.max_*_capacity).

    Fires ONLY when a DriverDaySchedule carries vehicle_cap; an uncapped schedule is
    untouched (the old blanket Guard A stays removed).
    """

    def _capped(self, pax=None, lug=None, slots=None):
        s = _fsched(9, slots or [])
        s.vehicle_cap = {"pax": pax, "lug": lug, "label": "SUV 009"}
        return s

    def test_over_passenger_cap_rejected(self):
        # SUV 009 capped at 5 pax; a 6-pax SUV leg can't go on it (even on an empty board).
        leg = _fleg(1, 9, 0, vtype="suv", pax=6)
        res = check_feasibility(self._capped(pax=5, lug=5), leg, _FB_D)
        self.assertFalse(res.feasible)
        self.assertIn("exceeds", res.reason)

    def test_at_passenger_cap_allowed(self):
        # Exactly at the cap (5/5) fits.
        leg = _fleg(1, 9, 0, vtype="suv", pax=5)
        leg.effective_luggage_count = 5
        res = check_feasibility(self._capped(pax=5, lug=5), leg, _FB_D)
        self.assertTrue(res.feasible)

    def test_over_luggage_cap_rejected(self):
        leg = _fleg(1, 9, 0, vtype="suv", pax=2)
        leg.effective_luggage_count = 6
        res = check_feasibility(self._capped(pax=5, lug=5), leg, _FB_D)
        self.assertFalse(res.feasible)

    def test_no_cap_set_does_not_block(self):
        # Without vehicle_cap, a 6-pax leg is fine (booking-time validation owns capacity).
        leg = _fleg(1, 9, 0, vtype="suv", pax=6)
        res = check_feasibility(_fsched(9, []), leg, _FB_D)
        self.assertTrue(res.feasible)

    def test_partial_cap_only_passengers(self):
        # Only a passenger cap set (lug=None): luggage is never blocked.
        leg = _fleg(1, 9, 0, vtype="suv", pax=4)
        leg.effective_luggage_count = 9
        res = check_feasibility(self._capped(pax=5, lug=None), leg, _FB_D)
        self.assertTrue(res.feasible)
