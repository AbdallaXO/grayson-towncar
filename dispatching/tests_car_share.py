"""The one co-driver car-share home (scheduling redesign, Build 3a — P2).

Build 3a moved every co-driver rule into ``dispatching/car_share.py`` and
pointed the engine, the manual-assign warnings, the mint engine and the write
door at it, WITHOUT changing a verdict. Two things therefore need locking down:

  1. the shared primitives behave exactly as the six literal copies they
     replaced (boundary semantics included — a touching pair must not overlap,
     or every tight-but-legal handoff on the board starts failing);
  2. the re-exports still resolve. ``scheduler.sharers_conflict`` /
     ``scheduler.build_sharer_partners`` are imported by ~12 modules and
     patched by name in tests_span_caps; ``assign_warnings.share_conflicts`` /
     ``build_share_entry`` are imported by the precision replay
     (docs/scheduling-redesign/analysis/12_warn_precision.py). If a re-export
     silently disappears, the replay stops matching the product and nothing
     else notices.

The three conventions are deliberately NOT unified — they disagree on 5 of 290
legs across the regime's shared unit-days (analysis/15_share_gate_divergence.py),
and picking a winner is the founder's call. The tests below pin each convention
to its own documented behaviour so a future "tidy-up" cannot quietly merge them.

Run with:  ./manage.py test dispatching.tests_car_share
"""
from datetime import datetime, timedelta, time as dt_time

from django.test import SimpleTestCase, TestCase

from dispatching import car_share as cs


class _Blk:
    """Minimum shape ``mint_share_ok`` consumes."""
    def __init__(self, pick, start, end):
        self.pick, self.start, self.end = pick, start, end


def _t(hh, mm=0):
    return datetime(2026, 6, 1, hh, mm)


class IntervalsOverlapTests(SimpleTestCase):
    """Half-open, and touching ends do NOT overlap. Six copies agreed on this;
    flipping it would reject every back-to-back pair on the board."""

    def test_plain_overlap(self):
        self.assertTrue(cs.intervals_overlap(_t(9), _t(11), _t(10), _t(12)))

    def test_disjoint(self):
        self.assertFalse(cs.intervals_overlap(_t(9), _t(10), _t(11), _t(12)))

    def test_touching_ends_do_not_overlap(self):
        self.assertFalse(cs.intervals_overlap(_t(9), _t(10), _t(10), _t(11)))
        self.assertFalse(cs.intervals_overlap(_t(10), _t(11), _t(9), _t(10)))

    def test_containment_both_directions(self):
        self.assertTrue(cs.intervals_overlap(_t(9), _t(13), _t(10), _t(11)))
        self.assertTrue(cs.intervals_overlap(_t(10), _t(11), _t(9), _t(13)))

    def test_is_symmetric(self):
        for a in (_t(8), _t(9), _t(10)):
            for b in (_t(9), _t(10), _t(11)):
                self.assertEqual(
                    cs.intervals_overlap(a, a + timedelta(hours=2), b, b + timedelta(hours=1)),
                    cs.intervals_overlap(b, b + timedelta(hours=1), a, a + timedelta(hours=2)))


class HoldersByUnitTests(SimpleTestCase):
    def test_groups_and_preserves_order(self):
        self.assertEqual(
            cs.holders_by_unit([(1, 7), (2, 8), (3, 7)]),
            {7: [1, 3], 8: [2]})

    def test_rows_without_a_vehicle_are_skipped(self):
        self.assertEqual(cs.holders_by_unit([(1, None), (2, 8)]), {8: [2]})

    def test_empty(self):
        self.assertEqual(cs.holders_by_unit([]), {})


class SharedConstantTests(SimpleTestCase):
    def test_two_drivers_per_vehicle_date(self):
        # Never observed above 2 in either regime (03 §2); the write door and
        # the mint engine both read this one number.
        self.assertEqual(cs.MAX_DRIVERS_PER_VEHICLE_DATE, 2)


class OccupancyBlockTests(SimpleTestCase):
    """One reader for the lead/tail table, and the percentile stays explicit —
    P50 for aggregate placement, P75 for single-leg feasibility (00 §A3.5)."""

    def test_p75_is_wider_than_p50(self):
        p50 = cs.occupancy_block(_t(10), "ARRIVAL", "p50")
        p75 = cs.occupancy_block(_t(10), "ARRIVAL", "p75")
        self.assertLess(p75[0], p50[0])
        self.assertGreater(p75[1], p50[1])

    def test_matches_the_handoff_chain_table(self):
        from dispatching.handoff_chain import OCCUPANCY_LEAD_TAIL_P50
        lead, tail = OCCUPANCY_LEAD_TAIL_P50["DEPARTURE"]
        start, end = cs.occupancy_block(_t(10), "DEPARTURE", "p50")
        self.assertEqual(start, _t(10) - timedelta(minutes=lead))
        self.assertEqual(end, _t(10) + timedelta(minutes=tail))


class MintShareOkTests(SimpleTestCase):
    """Convention C: overlap AND full one-sided separation — no interleaving at
    all. This strictness is what stopped the replay booking one car in two
    places (+4.0 -> +2.4 legs/day)."""

    def _leg(self, hh, span_h=1):
        return _Blk(_t(hh), _t(hh), _t(hh) + timedelta(hours=span_h))

    def test_no_partner_is_always_ok(self):
        self.assertTrue(cs.mint_share_ok(self._leg(10), [], 120))

    def test_overlap_is_rejected(self):
        self.assertFalse(
            cs.mint_share_ok(self._leg(10), [self._leg(10, 2)], 120))

    def test_clear_gap_after_the_partners_last_pickup_is_ok(self):
        self.assertTrue(
            cs.mint_share_ok(self._leg(14), [self._leg(8), self._leg(11)], 120))

    def test_clear_gap_before_the_partners_first_pickup_is_ok(self):
        self.assertTrue(
            cs.mint_share_ok(self._leg(6), [self._leg(8, 1), self._leg(11)], 120))

    def test_under_the_gap_on_both_sides_is_rejected(self):
        # Candidate picks up 11:30 — 90 min after the partner's last pickup
        # (10:00), gap is 120. Blocks do not overlap; the gap rule still bites.
        cand = _Blk(_t(11, 30), _t(11, 30), _t(12, 30))
        self.assertFalse(
            cs.mint_share_ok(cand, [self._leg(8), self._leg(10, 1)], 120))

    def test_exactly_the_gap_is_allowed(self):
        # >= gap, not > gap — the boundary the replay evidence was fitted on.
        self.assertTrue(
            cs.mint_share_ok(self._leg(12, 1), [self._leg(8), self._leg(10, 1)], 120))

    def test_interleaving_between_two_partner_jobs_is_rejected(self):
        # Sits in the middle: neither side clears the gap, even with no overlap.
        self.assertFalse(
            cs.mint_share_ok(self._leg(12), [self._leg(6), self._leg(18)], 120))


class ShareConflictsTests(SimpleTestCase):
    """Convention B: overlap + interleave + pickup-to-pickup pad, ADVISORY."""

    def _e(self, leg_id, did, hh, mm=0, span_h=1):
        return {"leg_id": leg_id, "did": did, "pick": _t(hh, mm),
                "start": _t(hh, mm), "end": _t(hh, mm) + timedelta(hours=span_h)}

    def test_same_driver_never_conflicts_with_itself(self):
        out = cs.share_conflicts([self._e(1, 10, 9), self._e(2, 10, 9, 30)], 120)
        self.assertEqual(out, [])

    def test_cross_driver_overlap_fires(self):
        out = cs.share_conflicts([self._e(1, 10, 9, span_h=3),
                                  self._e(2, 20, 10)], 120)
        self.assertIn("share_overlap", {c["code"] for c in out})

    def test_pad_fires_without_overlap(self):
        # 90 min apart, no block overlap (30-min jobs), pad 120.
        out = cs.share_conflicts([self._e(1, 10, 9, span_h=0),
                                  self._e(2, 20, 10, 30, span_h=0)], 120)
        codes = {c["code"] for c in out}
        self.assertIn("share_pad", codes)
        self.assertNotIn("share_overlap", codes)

    def test_one_handback_is_allowed_two_is_interleave(self):
        one = cs.share_conflicts([self._e(1, 10, 6, span_h=0),
                                  self._e(2, 20, 14, span_h=0)], 120)
        self.assertNotIn("share_interleave", {c["code"] for c in one})
        two = cs.share_conflicts([self._e(1, 10, 6, span_h=0),
                                  self._e(2, 20, 12, span_h=0),
                                  self._e(3, 10, 18, span_h=0)], 120)
        self.assertIn("share_interleave", {c["code"] for c in two})

    def test_focus_leg_scopes_the_verdicts(self):
        entries = [self._e(1, 10, 9, span_h=3), self._e(2, 20, 10),
                   self._e(3, 10, 20, span_h=0)]
        out = cs.share_conflicts(entries, 120, focus_leg_id=3)
        self.assertNotIn("share_overlap", {c["code"] for c in out},
                         "a pre-existing overlap between two OTHER legs must "
                         "not be blamed on the leg being assigned")


class ReExportTests(SimpleTestCase):
    """The old import paths must keep resolving to the SAME objects — ~12
    product modules and both replay scripts depend on them."""

    def test_scheduler_reexports_the_engine_gate(self):
        import dispatching.scheduler as sch
        self.assertIs(sch.sharers_conflict, cs.sharers_conflict)
        self.assertIs(sch.build_sharer_partners, cs.build_sharer_partners)

    def test_assign_warnings_reexports_the_warning_core(self):
        import dispatching.assign_warnings as aw
        self.assertIs(aw.share_conflicts, cs.share_conflicts)
        self.assertIs(aw.build_share_entry, cs.build_share_entry)

    def test_standby_mints_uses_the_shared_rule_and_constant(self):
        import dispatching.standby_mints as sm
        self.assertIs(sm.mint_share_ok, cs.mint_share_ok)
        self.assertIs(sm.intervals_overlap, cs.intervals_overlap)
        self.assertEqual(sm.MAX_DRIVERS_PER_VEHICLE_DATE,
                         cs.MAX_DRIVERS_PER_VEHICLE_DATE)

    def test_car_share_imports_without_django_models_at_module_level(self):
        # standby_mints and the analysis scripts import this module without
        # booting Django; every ORM touch must stay inside a function.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(cs))
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(top, [], "car_share must have no module-level imports")


class SharersConflictDefaultPadTests(TestCase):
    """sharers_conflict's default (pad_min=None) reads
    SchedulerSettings.engine_share_pad_min — a DEDICATED dial split from
    vehicle_share_pad_min on 2026-08-24 (05_BUILD3B_TICKETS.md §9.1/9.2).

    The split matters because this convention measures its pad from the
    candidate's own estimated CLEAR time forward, not pickup-to-pickup, so
    the same numeric pad is a materially stricter test here than B/C's —
    strict enough, at the shared 120, to farm out real handoffs the founder
    ground-truthed as fine. These tests set the two settings to DIFFERENT
    values so a regression that reverts to reading vehicle_share_pad_min
    fails loudly rather than by coincidence."""

    @classmethod
    def setUpTestData(cls):
        from dispatching.scheduler import preload_timing_cache
        preload_timing_cache()

    def setUp(self):
        from dispatching.models import SchedulerSettings
        SchedulerSettings.clear_cache()
        self.addCleanup(SchedulerSettings.clear_cache)

    def _partner_slot(self, leg_id, pickup, end):
        from dispatching.scheduler import ScheduleSlot
        return ScheduleSlot(
            leg_id=leg_id, pickup_time=pickup,
            pickup_location="Disney Contemporary", pickup_category="Disney Resort",
            dropoff_location="MCO", dropoff_category="MCO Terminal",
            trip_type="departure", estimated_end_time=end,
            reservation_id=0, customer_name="", status="in-progress",
            has_flight=False)

    def _fixture(self, engine_pad, vehicle_pad):
        from dispatching.models import SchedulerSettings
        from dispatching.scheduler import DriverDaySchedule
        from dispatching.tests_swap_guards import fake_leg
        cfg = SchedulerSettings.get_settings()
        cfg.engine_share_pad_min = engine_pad
        cfg.vehicle_share_pad_min = vehicle_pad   # deliberately different
        cfg.save()
        # Reproduces the real 2026-04-10 case: driver clears ~13:06, partner
        # picks up 15:00 — a 114-min clear-to-pickup gap. At 120 that's a
        # conflict; at 65 (the fixed default) it is not.
        candidate = fake_leg(leg_id=1, pickup=dt_time(12, 30))
        partner_slot = self._partner_slot(
            2, dt_time(15, 0), datetime(2026, 4, 10, 15, 35))
        schedules = {2: DriverDaySchedule(
            driver_id=2, driver_name="Partner", driver_type="inhouse",
            slots=[partner_slot])}
        return candidate, {1: {2}, 2: {1}}, schedules

    def test_default_pad_reads_engine_share_pad_min_not_vehicle_share_pad_min(self):
        candidate, sharer_partners, schedules = self._fixture(
            engine_pad=65, vehicle_pad=120)
        self.assertFalse(
            cs.sharers_conflict(candidate, 1, sharer_partners, schedules,
                                datetime(2026, 4, 10).date()),
            "with engine_share_pad_min=65 this real, founder-confirmed-fine "
            "handoff must NOT be flagged — if this fails, the default pad "
            "selection has regressed to reading vehicle_share_pad_min")

    def test_explicit_pad_min_still_overrides_both_settings(self):
        # Callers that pass pad_min explicitly (the replay scripts, the
        # precision gate) must be completely unaffected by either setting.
        candidate, sharer_partners, schedules = self._fixture(
            engine_pad=65, vehicle_pad=120)
        self.assertTrue(
            cs.sharers_conflict(candidate, 1, sharer_partners, schedules,
                                datetime(2026, 4, 10).date(), pad_min=200),
            "an explicit pad_min must win over both DB settings")

    def test_default_value_is_65(self):
        from dispatching.models import SchedulerSettings
        self.assertEqual(SchedulerSettings.get_settings().engine_share_pad_min, 65)
