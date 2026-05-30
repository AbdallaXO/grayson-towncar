"""Unit tests for dispatching.feasibility_guards (pure functions, no DB).

Guard A (capacity_fit) was removed — booking-time validation enforces capacity — so only
Guard B (turnaround) and Guard C (window) logic is tested here.
"""
from datetime import datetime, date, time
from unittest.mock import patch
from django.test import SimpleTestCase

from dispatching import feasibility_guards as fg


class TurnaroundTests(SimpleTestCase):
    def test_non_arrival_full_drive_plus_pad(self):
        # anything -> Port (non-arrival): full drive + 10 pad
        self.assertEqual(fg.required_turnaround(30, next_is_airport_arrival=False, same_terminal=False), 40)

    def test_airport_arrival_same_terminal(self):
        # airport drop -> airport arrival same terminal: ~0 reposition + pad
        self.assertEqual(fg.required_turnaround(45, next_is_airport_arrival=True, same_terminal=True), 10)

    def test_airport_arrival_from_resort(self):
        # resort -> airport arrival: drive - deplaning(20) + pad(10)
        self.assertEqual(fg.required_turnaround(45, next_is_airport_arrival=True, same_terminal=False), 45 - 20 + 10)

    def test_airport_arrival_drive_under_grace_floors_zero(self):
        self.assertEqual(fg.required_turnaround(10, next_is_airport_arrival=True, same_terminal=False), 0 + 10)

    def test_custom_params(self):
        self.assertEqual(
            fg.required_turnaround(60, next_is_airport_arrival=True, same_terminal=False,
                                   deplaning_grace=30, safety_pad=5),
            60 - 30 + 5)

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

    def test_flexible_still_bound_by_clear_by(self):
        # FLEXIBLE_RESPECTS_CLEAR_BY default True: flex driver still can't clear past end
        ok, reason = fg.window_check(self.W(end=17, flexible=True), time(16, 0), datetime(2026, 5, 1, 18, 30), 3)
        self.assertFalse(ok)
        self.assertIn("clear-by", reason)

    def test_flexible_bypasses_end_when_disabled(self):
        ok, _ = fg.window_check(self.W(end=17, flexible=True), time(16, 0), datetime(2026, 5, 1, 18, 30), 3,
                                flexible_respects_clear_by=False)
        self.assertTrue(ok)

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
        self.assertIsNone(fg.get_effective_window(999999))

    def test_stub_false_uses_configured(self):
        # H2: flipping USE_STUB_WINDOWS=False must switch to the configured window,
        # NOT silently disable the guard (return None).
        configured = {"start": 6, "end": 17, "max_hours": 12.0, "flexible": False}
        with patch.object(fg, "USE_STUB_WINDOWS", False):
            self.assertEqual(fg.get_effective_window(46, configured=configured), configured)
            self.assertIsNone(fg.get_effective_window(46, configured=None))
