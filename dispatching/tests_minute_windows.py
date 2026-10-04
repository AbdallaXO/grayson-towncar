"""Minute, cross-midnight and base->base driver windows in the rules door (Stage 1, Task 1).

A window dict may carry start_min/end_min (minutes after 00:00 of the target date; over
1440 = next day), kind ("morning"/"midday"/"evening") and source="regular". Those windows
are checked base -> base through handoff_chain.shift_lead_min / shift_tail_min. Hour
windows carry none of these keys and must behave byte-for-byte as before — the grid test
pins that against a verbatim copy of the pre-Stage-1 window_check.

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_minute_windows
"""
from datetime import date, datetime, time, timedelta
from datetime import time as dt_time

from django.test import SimpleTestCase

from dispatching import feasibility_guards as fg
from dispatching import handoff_chain as hc
from dispatching.feasibility_guards import (END_HOUR_MODE, FLEXIBLE_RESPECTS_CLEAR_BY,
                                            NIGHT_LEG_FLEX_BLOCK, NIGHT_LEG_BOUNDARY_HOUR)


# ── window_check exactly as it shipped before Stage 1 (verbatim; do not edit) ──
def _legacy_window_check(window, pickup_time, clear_dt, span_hours_after,
                         target_date=None, mode=None, flexible_respects_clear_by=None,
                         span_hours_before=None):
    """(ok, reason) for whether adding a leg respects the driver's window + max_hours.

    window: {"start", "end", "max_hours", "flexible"}; None => skip.
    pickup_time: datetime.time of the new leg's pickup.
    clear_dt: datetime when the new leg clears (finishes).
    span_hours_after: driver's day span (first pickup -> last clear) IF this leg is added.
    target_date: the schedule date; used to build an ABSOLUTE clear-by datetime so a leg
        that clears AFTER MIDNIGHT (e.g. a 22:30 pickup clearing 00:30 next day) is correctly
        judged a clear-by violation rather than evading it via a bare hour comparison.
    span_hours_before: the day span WITHOUT the leg. When the day ALREADY exceeds max_hours
        (a pre-existing/manual board, or a modal cap set below the saved day), only inserts
        that GROW the span are blocked — hole-fills that leave the span unchanged stay legal,
        so an over-cap driver isn't frozen out of every insert. None => legacy total-span gate.
    """
    if not window:
        return True, ""
    mode = END_HOUR_MODE if mode is None else mode
    frcb = FLEXIBLE_RESPECTS_CLEAR_BY if flexible_respects_clear_by is None else flexible_respects_clear_by
    flexible = bool(window.get("flexible", False))
    start = window.get("start")
    end = window.get("end")
    max_h = window.get("max_hours")

    # START bound — bypassed for flexible drivers (flexible on start time).
    if not flexible and start is not None and pickup_time.hour < start:
        return False, f"pickup {pickup_time.strftime('%H:%M')} before start {start}:00"

    # NIGHT bound — a 00:00-(boundary-1):59 pickup is night duty: Flexible means "any time
    # within a normal day", not "also works the middle of the night". Two escapes:
    # (1) an EXPLICIT start that covers the hour — explicitness beats the flexible flag,
    #     so a dispatcher typing From=00:00 in the builder, an accepted advisor night
    #     card (planned_start_hour=0), or a stub observed working nights still seats;
    # (2) night_exempt windows (get_effective_window(enforce_cap=False) — the manual-
    #     sovereign paths): an intentional dispatcher move is never hard-blocked, and a
    #     pre-existing night leg must not poison execute_swap's all-legs revalidation.
    if (flexible and NIGHT_LEG_FLEX_BLOCK
            and pickup_time.hour < NIGHT_LEG_BOUNDARY_HOUR
            and not window.get("night_exempt")
            and not (start is not None and start <= pickup_time.hour)):
        return False, (f"night pickup {pickup_time.strftime('%H:%M')} needs an explicit "
                       f"night-shift window (Flexible does not cover "
                       f"00:00-0{NIGHT_LEG_BOUNDARY_HOUR}:00)")

    # END bound.
    if end is not None:
        if mode == "CLEAR_BY":
            enforce_end = (not flexible) or frcb
            if enforce_end and clear_dt is not None:
                # must FINISH by end:00 (a clear exactly at end:00 is OK).
                if target_date is not None:
                    clear_by_dt = datetime.combine(target_date, dt_time(min(int(end), 23), 0))
                    over = clear_dt > clear_by_dt   # correctly catches next-day (after-midnight) clears
                else:  # same-day fallback only (no date supplied)
                    over = clear_dt.hour > end or (clear_dt.hour == end and clear_dt.minute > 0)
                if over:
                    return False, f"clears {clear_dt.strftime('%H:%M')} after clear-by {end}:00"
        else:  # LAST_PICKUP
            if not flexible and pickup_time.hour > end:
                return False, f"pickup {pickup_time.strftime('%H:%M')} after last-pickup {end}:00"

    # MAX HOURS — hard cap on day span (wall-clock first pickup -> last clear).
    if max_h is not None and span_hours_after is not None and span_hours_after > float(max_h):
        already_over = (span_hours_before is not None
                        and span_hours_before > float(max_h))
        grows = (span_hours_before is None
                 or span_hours_after > span_hours_before + 1e-9)
        if not already_over or grows:
            return False, f"day span {span_hours_after:.1f}h > max_hours {max_h}"

    return True, ""


D = date(2026, 10, 7)


def W(start=None, end=None, **extra):
    return {"start": start, "end": end, "max_hours": None, "flexible": False, **extra}


def dt(h, m, day=0):
    return datetime.combine(D + timedelta(days=day), time(h, m))


class HourWindowParityTests(SimpleTestCase):
    """Hour windows (no start_min) must take the untouched hour path."""

    def test_hour_windows_match_legacy_on_grid(self):
        # EVERY hour window: start in (None, 0..23) x end in (None, 0..23) — wrapping ones
        # included (an approved day off makes (stub_start, 0)); pickups 00:00-23:45 step 15;
        # clears +0..+300 step 30; modes CLEAR_BY/LAST_PICKUP; flexible True/False;
        # target_date D and None.
        hours = [None] + list(range(24))
        windows = [W(start=s, end=e, flexible=f)
                   for s in hours for e in hours for f in (False, True)]
        cases = []
        for minute in range(0, 24 * 60, 15):
            p = time(minute // 60, minute % 60)
            base = datetime.combine(D, p)
            for k in range(0, 301, 30):
                cases.append((p, base + timedelta(minutes=k)))
        new, old = fg.window_check, _legacy_window_check
        mismatches = 0
        first = None
        for w in windows:
            for p, c in cases:
                for m in ("CLEAR_BY", "LAST_PICKUP"):
                    for td in (D, None):
                        got = new(w, p, c, 5.0, target_date=td, mode=m)
                        want = old(w, p, c, 5.0, target_date=td, mode=m)
                        if got != want:
                            mismatches += 1
                            if first is None:
                                first = (w, p, c, m, td, got, want)
        self.assertEqual(mismatches, 0, f"first mismatch: {first}")

    def test_hour_window_wrap_unchanged(self):
        self.assertEqual(fg.window_check(W(start=6, end=0), time(22, 0), dt(23, 0), 1, target_date=D),
                         (False, "clears 23:00 after clear-by 0:00"))


class MinuteWindowTests(SimpleTestCase):
    """The minute path: start_min/end_min, base->base lead and tail, cross-midnight ends."""

    def test_minute_start_without_categories(self):
        w = W(start=4, end=17, start_min=250, end_min=970, kind="morning", source="regular")
        self.assertEqual(fg.window_check(w, time(4, 5), dt(5, 0), 1, target_date=D),
                         (False, "pickup 04:05 before start 04:10"))
        self.assertTrue(fg.window_check(w, time(4, 10), dt(5, 0), 1, target_date=D)[0])

    def test_minute_lead_uses_pickup_zone(self):
        w = W(start=4, end=17, start_min=275, end_min=995, kind="morning", source="regular")  # 04:35
        self.assertTrue(fg.window_check(w, time(5, 0), dt(6, 0), 1, target_date=D,
                                        pickup_category="MCO Terminal",
                                        dropoff_category="Disney Resort")[0])
        self.assertEqual(fg.window_check(w, time(5, 0), dt(6, 0), 1, target_date=D,
                                         pickup_category="Disney Resort",
                                         dropoff_category="MCO Terminal"),
                         (False, "pickup 05:00 means leaving base 04:10, before start 04:35"))

    def test_minute_tail_morning_fuel(self):
        w = W(start=4, end=17, start_min=275, end_min=995, kind="morning", source="regular")  # ends 16:35
        self.assertTrue(fg.window_check(w, time(15, 0), dt(16, 8), 1, target_date=D,
                                        pickup_category="Disney Resort",
                                        dropoff_category="MCO Terminal")[0])
        self.assertEqual(fg.window_check(w, time(15, 0), dt(16, 9), 1, target_date=D,
                                         pickup_category="Disney Resort",
                                         dropoff_category="MCO Terminal"),
                         (False, "clears 16:09, back at base 16:36, after 16:35"))

    def test_cross_midnight_evening_tail(self):
        w = W(start=14, end=23, start_min=855, end_min=1575, kind="evening", source="regular")  # ends 02:15
        self.assertTrue(fg.window_check(w, time(23, 30), dt(1, 14, day=1), 1, target_date=D,
                                        pickup_category="MCO Terminal",
                                        dropoff_category="MCO Terminal")[0])
        self.assertEqual(fg.window_check(w, time(23, 30), dt(1, 15, day=1), 1, target_date=D,
                                         pickup_category="MCO Terminal",
                                         dropoff_category="MCO Terminal"),
                         (False, "clears 01:15, back at base 02:16, after 02:15 (next day)"))

    def test_cross_midnight_without_target_date(self):
        w = W(start=14, end=23, start_min=855, end_min=1575, kind="evening", source="regular")
        self.assertTrue(fg.window_check(w, time(22, 30), dt(0, 45, day=1), 1)[0])
        self.assertEqual(fg.window_check(w, time(23, 50), dt(2, 16, day=1), 1),
                         (False, "clears 02:16 after clear-by 02:15 (next day)"))

    def test_lead_before_midnight_says_day_before(self):
        # An evening lead at Disney is 75 min, so a 00:20 pickup means leaving base the
        # evening before — the reason says so instead of a bare, wrapped 23:05.
        w = W(start=0, end=12, start_min=10, end_min=730, kind="evening", source="regular")
        self.assertEqual(fg.window_check(w, time(0, 20), dt(1, 0), 1, target_date=D,
                                         pickup_category="Disney Resort",
                                         dropoff_category="MCO Terminal"),
                         (False, "pickup 00:20 means leaving base 23:05 (day before), "
                                 "before start 00:10"))

    def test_half_minute_window_takes_hour_path(self):
        # start_min without end_min (or end_min None) is not a minute window: it falls back
        # to the hour path instead of raising inside the scheduler's loop.
        for extra in ({"start_min": 250}, {"start_min": 250, "end_min": None}):
            w = W(start=4, end=17, kind="morning", source="regular", **extra)
            for p, c in ((time(4, 5), dt(5, 0)), (time(3, 59), dt(5, 0)), (time(16, 0), dt(17, 1))):
                self.assertEqual(fg.window_check(w, p, c, 1, target_date=D),
                                 _legacy_window_check(W(start=4, end=17), p, c, 1, target_date=D))

    def test_minute_last_pickup_mode(self):
        w = W(start=4, end=16, start_min=250, end_min=930, kind="midday", source="regular")
        self.assertTrue(fg.window_check(w, time(15, 30), dt(16, 0), 1, target_date=D,
                                        mode="LAST_PICKUP")[0])
        self.assertEqual(fg.window_check(w, time(15, 31), dt(16, 0), 1, target_date=D,
                                         mode="LAST_PICKUP"),
                         (False, "pickup 15:31 after last-pickup 15:30"))

    def test_minute_flexible_semantics_match_hours(self):
        w = W(start=1, end=10, start_min=90, end_min=600, kind="midday", source="regular",
              flexible=True)
        self.assertTrue(fg.window_check(w, time(1, 30), dt(11, 0), 1, target_date=D)[0])   # escape + no clear-by
        self.assertFalse(fg.window_check(w, time(1, 15), dt(2, 0), 1, target_date=D)[0])   # night rule

    def test_minute_max_hours_matches_hours(self):
        # The max-hours block is unchanged: same verdict and text as an hour window,
        # including the already-over hole-fill escape.
        mw = W(start=4, end=17, start_min=250, end_min=970, kind="morning", source="regular",
               max_hours=10)
        hw = W(start=4, end=17, max_hours=10)
        for after, before in ((9.5, None), (10.5, None), (10.5, 11.0), (11.5, 11.0), (10.5, 9.0)):
            self.assertEqual(
                fg.window_check(mw, time(6, 0), dt(7, 0), after, target_date=D,
                                span_hours_before=before),
                _legacy_window_check(hw, time(6, 0), dt(7, 0), after, target_date=D,
                                     span_hours_before=before))
        self.assertEqual(fg.window_check(mw, time(6, 0), dt(7, 0), 10.5, target_date=D),
                         (False, "day span 10.5h > max_hours 10"))


class RegularWindowTests(SimpleTestCase):
    """source="regular" bypasses the observed-history stub for that one driver."""

    def test_regular_window_bypasses_stub(self):              # id 46 is stubbed 6-20, 14h
        cfg = {"start": 4, "end": 17, "start_min": 250, "end_min": 970, "kind": "morning",
               "source": "regular", "max_hours": None, "flexible": False}
        w = fg.get_effective_window(46, configured=cfg)
        self.assertEqual((w["start"], w["end"], w["start_min"], w["end_min"]), (4, 17, 250, 970))
        self.assertEqual(w["max_hours"], fg.SPAN_HARD_HOURS_DEFAULT)
        self.assertFalse(w["night_exempt"])

    def test_regular_hour_window_bypasses_stub(self):         # retyped modal hours (S9)
        w = fg.get_effective_window(46, configured={"start": 4, "end": 22, "max_hours": None,
                                                    "flexible": False, "source": "regular"})
        self.assertEqual((w["start"], w["end"]), (4, 22))
        self.assertNotIn("start_min", w)

    def test_regular_window_manual_sovereign(self):
        w = fg.get_effective_window(46, configured={
            "start": 4, "end": 17, "start_min": 250, "end_min": 970, "kind": "morning",
            "source": "regular", "max_hours": None, "flexible": False}, enforce_cap=False)
        self.assertTrue(w["night_exempt"])
        self.assertIsNone(w["max_hours"])

    def test_regular_window_keys(self):
        self.assertEqual(fg.regular_window_keys({}), {})
        self.assertEqual(fg.regular_window_keys({"window_start_min": None, "window_end_min": 930}), {})
        self.assertEqual(fg.regular_window_keys({"window_start_min": 250, "window_end_min": 970,
                                                 "window_kind": "morning"}),
                         {"start_min": 250, "end_min": 970, "kind": "morning", "source": "regular"})

    def test_legacy_hours(self):
        self.assertEqual(fg.legacy_hours(250, 970), (4, 17))
        self.assertEqual(fg.legacy_hours(855, 1575), (14, 23))
        self.assertEqual(fg.legacy_hours(420, 1140), (7, 19))


class ChainOkMinuteWindowTests(SimpleTestCase):
    """scheduler._chain_ok hands each slot's own pickup / drop zones to window_check."""

    WINDOW = {"start": 4, "end": 17, "start_min": 275, "end_min": 995, "kind": "morning",
              "source": "regular", "max_hours": None, "flexible": False}  # 04:35-16:35

    def _day(self, pickup_cat, dropoff_cat, trip_type):
        from dispatching.scheduler import DriverDaySchedule, ScheduleSlot
        slot = ScheduleSlot(
            leg_id=1, pickup_time=time(5, 0), pickup_location=pickup_cat,
            pickup_category=pickup_cat, dropoff_location=dropoff_cat,
            dropoff_category=dropoff_cat, trip_type=trip_type,
            estimated_end_time=dt(6, 0), reservation_id=1, customer_name="Test",
            status="scheduled", has_flight=False)
        return DriverDaySchedule(driver_id=1, driver_name="Test", driver_type="employee",
                                 slots=[slot])

    def test_chain_ok_reads_pickup_zone_for_lead(self):
        from dispatching import scheduler
        # Disney pickup: 35 drive + 15 buffer = leave base 04:10, before 04:35.
        self.assertFalse(scheduler._chain_ok(
            self._day("Disney Resort", "MCO Terminal", "departure"), D,
            driver_window=self.WINDOW))
        # MCO pickup: 12 drive + 10 buffer = leave base 04:38.
        self.assertTrue(scheduler._chain_ok(
            self._day("MCO Terminal", "Disney Resort", "arrival"), D,
            driver_window=self.WINDOW))


class ShiftLeadTailTests(SimpleTestCase):
    """handoff_chain's base->base lead and tail (07 §6.1)."""

    def test_shift_lead_tail(self):
        self.assertEqual(hc.shift_lead_min("morning", "MCO Terminal"), 22)
        self.assertEqual(hc.shift_tail_min("morning", "MCO Terminal"), 27)
        self.assertEqual(hc.shift_tail_min("evening", "MCO Terminal"), 61)
        self.assertEqual(hc.shift_lead_min("evening", "Disney Resort"), 75)
