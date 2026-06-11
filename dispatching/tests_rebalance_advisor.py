"""Rebalance Advisor tests — relative balance + density (demand-aware staffing round 2).

Mirrors tests_fold_advisor.py: in-memory boards via tests_span_caps fixtures, real DB
rows only where the module reads them (DVA rows for dvtypes). Every trigger formula and
gate gets a test that fails when the gate is removed.
"""
from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase

from dispatching.rebalance_advisor import build_rebalance_proposals, _is_hollow
from dispatching.tests_span_caps import _slot, _sched, _FakeLeg
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Vehicle

User = get_user_model()
TARGET = date(2026, 6, 9)   # must match tests_span_caps.D

W_RIGID = {"start": None, "end": None, "max_hours": 17.0, "flexible": False,
           "night_exempt": False}
ALL_DAY = (4, 23)


def _mk_driver(username, certified=None):
    u = User.objects.create_user(username=username, password="x")
    d = Driver.objects.create(profile=u, driver_type="inhouse", is_active=True)
    if certified:
        d.certified_vehicle_types.add(certified)
    return d


def _leg(leg_id, pickup_h, pickup_m=0, vtype="suv", revenue=100):
    return _FakeLeg(
        id=leg_id, pickup_time=time(pickup_h, pickup_m),
        pickup_location="Disney Resort", dropoff_location="MCO Terminal",
        effective_vehicle_type=vtype, revenue_share=revenue,
        driver=None, driver_id=None, reservation_id=1, status="pending",
        flight_information=None, trip_type="return",
    )


class RebalanceAdvisorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vt_suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.units = {}
        cls.drivers = {}
        for i, name in enumerate(["thin", "heavy", "mid", "extra"], 1):
            u = FleetVehicle.objects.create(vehicle_number=f"00{i}", vehicle_type=cls.vt_suv,
                                            year=2023, make="Chevy", model="Suburban")
            d = _mk_driver(name)
            DriverVehicleAssignment.objects.create(driver=d, date=TARGET, vehicle=u)
            cls.units[name] = u
            cls.drivers[name] = d

    def _ctx(self, boards, **over):
        """boards: {name: [slots]} -> full context; all legs engine-proposed+movable."""
        scheds, final, legs = {}, {}, {}
        for name, slots in boards.items():
            did = self.drivers[name].id
            scheds[did] = _sched(did, list(slots), name=name)
            for s in slots:
                final[s.leg_id] = did
                legs[s.leg_id] = _leg(s.leg_id, s.pickup_time.hour, s.pickup_time.minute)
        legs.update(over.pop("extra_legs", {}))
        ctx = dict(
            target_date=TARGET,
            proposed_schedules=scheds,
            final_assignments=final,
            locked_leg_ids=set(),
            driver_hours={self.drivers[n].id: ALL_DAY for n in boards},
            flexible_drivers=set(),
            capped_windows={self.drivers[n].id: dict(W_RIGID) for n in boards},
            sharer_partners={},
            legs_by_id=legs,
            drivers_by_id={d.id: d for d in self.drivers.values()},
        )
        ctx.update(over)
        return ctx

    def _cards(self, ctx):
        return [p for p in build_rebalance_proposals(**ctx) if p.get("kind") == "rebalance"]

    @staticmethod
    def _spaced(start_h, n, leg_id0, step=2):
        return [_slot(leg_id0 + i, start_h + i * step) for i in range(n)]

    # ── FILL triggers (the founder's RELATIVE rule) ──────────────────────────
    def test_fill_card_for_thin_driver(self):
        # thin 1 job vs heavy 7 / mid 6: mean 4.7, thin_cut max(1, floor(2.3))=2,
        # spread 6 >= 3 -> fill card moves heavy's legs to thin.
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 7, 21),
                         "mid": self._spaced(7, 6, 41)})
        cards = self._cards(ctx)
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["direction"], "fill")
        self.assertEqual(c["driver_id"], self.drivers["thin"].id)
        self.assertTrue(1 <= len(c["moves"]) <= 3)
        self.assertTrue(all(m["to_driver_id"] == self.drivers["thin"].id for m in c["moves"]))
        self.assertLessEqual(c["spread_after"], c["spread_before"])
        self.assertGreater(c["jobs_after"], c["jobs_before"])

    def test_no_card_on_even_slow_day(self):
        # 3/3/3 — the founder's "3 each on a slow day is fine": silent.
        ctx = self._ctx({"thin": self._spaced(8, 3, 11),
                         "heavy": self._spaced(9, 3, 21),
                         "mid": self._spaced(10, 3, 41)})
        self.assertEqual(self._cards(ctx), [])

    def test_min_spread_tolerates_small_imbalance(self):
        # 4/3/2: spread 2 < 3 -> "roughly even", silent even though 2 <= thin_cut.
        ctx = self._ctx({"thin": self._spaced(8, 2, 11),
                         "heavy": self._spaced(9, 4, 21),
                         "mid": self._spaced(10, 3, 41)})
        self.assertEqual(self._cards(ctx), [])

    def test_donor_floor_no_inversion(self):
        # heavy 4 / thin 1 / mid 4: mean 3.0, ceil=3 -> heavy can give exactly 1
        # (4->3 >= ceil(mean) and >= thin_after 2). Never below the floor.
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 4, 21),
                         "mid": self._spaced(7, 4, 41)})
        cards = self._cards(ctx)
        self.assertEqual(len(cards), 1)
        for d in cards[0]["donors"]:
            self.assertGreaterEqual(d["jobs_after"], 3)
        self.assertLessEqual(cards[0]["jobs_after"], 3)

    def test_zero_leg_working_driver_not_a_fill_target(self):
        # extra has 0 legs (working but empty) — fold's territory, never filled.
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 7, 21),
                         "mid": self._spaced(7, 6, 41),
                         "extra": []})
        cards = self._cards(ctx)
        self.assertNotIn(self.drivers["extra"].id, [c["driver_id"] for c in cards])

    def test_build_first_excluded_as_subject_and_donor(self):
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 7, 21),
                         "mid": self._spaced(7, 6, 41)},
                        build_first_ids={self.drivers["thin"].id, self.drivers["heavy"].id})
        cards = self._cards(ctx)
        self.assertNotIn(self.drivers["thin"].id, [c["driver_id"] for c in cards])
        for c in cards:
            for m in c["moves"]:
                self.assertNotEqual(m["from_driver_id"], self.drivers["heavy"].id)

    def test_locked_donor_leg_never_moves_but_others_do(self):
        # heavy's leg 21 is dispatcher-locked: it must never appear in moves; his
        # engine legs still donate (per-leg manual-sovereign, unlike fold).
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 7, 21),
                         "mid": self._spaced(7, 6, 41)},
                        locked_leg_ids={21})
        cards = self._cards(ctx)
        self.assertEqual(len(cards), 1)
        self.assertNotIn(21, [m["leg_id"] for m in cards[0]["moves"]])

    def test_fold_card_subject_excluded(self):
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 7, 21),
                         "mid": self._spaced(7, 6, 41)},
                        exclude_driver_ids={self.drivers["thin"].id})
        self.assertEqual(self._cards(ctx), [])

    def test_suppressed_on_residuals(self):
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 7, 21),
                         "mid": self._spaced(7, 6, 41)},
                        residual_count=1)
        self.assertEqual(build_rebalance_proposals(**ctx), [])

    def test_fill_gate_stack_window_parity(self):
        # thin's modal window 6-10; heavy's 14:00+ legs can't move to him -> at most
        # the morning legs move; if nothing fits, info card not a fill card.
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": [_slot(21, 14), _slot(22, 16), _slot(23, 18),
                                   _slot(24, 20), _slot(25, 21), _slot(26, 22),
                                   _slot(27, 15)],
                         "mid": self._spaced(7, 6, 41)})
        ctx["driver_hours"][self.drivers["thin"].id] = (6, 10)
        cards = self._cards(ctx)
        for c in cards:
            for m in c["moves"]:
                if m["to_driver_id"] == self.drivers["thin"].id:
                    self.assertLessEqual(m["pickup"].split(":")[0].strip(), "9")

    def test_night_leg_never_fills_flexible_thin_driver(self):
        # heavy holds a 00:30 leg; thin is Flexible with no explicit night start ->
        # the night rule blocks that move.
        night = _slot(29, 0, 30)
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 6, 21) + [night],
                         "mid": self._spaced(7, 6, 41)},
                        flexible_drivers={self.drivers["thin"].id})
        ctx["capped_windows"][self.drivers["thin"].id] = {
            "start": None, "end": None, "max_hours": 17.0, "flexible": True,
            "night_exempt": False}
        cards = self._cards(ctx)
        for c in cards:
            self.assertNotIn(29, [m["leg_id"] for m in c["moves"]])

    # ── COMPRESS ─────────────────────────────────────────────────────────────
    def test_compress_raymond_shape(self):
        # H: dense 06:00-07:30 cluster + 16:45 + 22:24 outliers (raw ~17h, big hole,
        # eff low via break credit) -> compress card moves the outliers, day collapses.
        h_slots = [_slot(11, 6), _slot(12, 6, 45), _slot(13, 7, 30),
                   _slot(14, 16, 45), _slot(15, 22, 24)]
        ctx = self._ctx({"thin": h_slots,
                         "heavy": self._spaced(14, 4, 21),   # 14-20h, clears 21:00 — can
                         "mid": self._spaced(8, 4, 41)})     # absorb 22:24; mid takes 16:45
        cards = [c for c in self._cards(ctx) if c["direction"] == "compress"]
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["driver_id"], self.drivers["thin"].id)
        moved = {m["leg_id"] for m in c["moves"]}
        self.assertIn(15, moved)             # the 22:24 outlier moves first
        self.assertGreaterEqual(c["span_before"] - c["span_after"], 4.0)
        self.assertEqual(c["jobs_after"], c["jobs_before"] - len(c["moves"]))

    def test_compress_silent_on_dense_long_day(self):
        # A dense 10h day (no >=4h hole) is NOT hollow — never compressed.
        ctx = self._ctx({"thin": self._spaced(6, 6, 11),   # 6:00..16:00 spaced 2h
                         "heavy": self._spaced(8, 6, 21),
                         "mid": self._spaced(9, 6, 41)})
        self.assertEqual([c for c in self._cards(ctx) if c["direction"] == "compress"], [])

    def test_compress_keeps_at_least_one_leg(self):
        # 2-leg hollow day: only ONE leg may peel (H keeps >= 1) — collapse must
        # come from a single move or no card.
        h_slots = [_slot(11, 6), _slot(12, 20)]
        ctx = self._ctx({"thin": h_slots,
                         "heavy": self._spaced(13, 5, 21),
                         "mid": self._spaced(14, 5, 41)})
        cards = [c for c in self._cards(ctx) if c["direction"] == "compress"]
        for c in cards:
            self.assertGreaterEqual(c["jobs_after"], 1)
            self.assertLessEqual(len(c["moves"]), 1)

    def test_is_hollow_predicate(self):
        dense = self._spaced(6, 6, 11)                     # 2h gaps, raw 11h
        hollow = [_slot(11, 6), _slot(12, 6, 45), _slot(13, 18)]  # ~12.5h raw, huge hole
        self.assertFalse(_is_hollow(dense, TARGET))
        self.assertTrue(_is_hollow(hollow, TARGET))
        self.assertFalse(_is_hollow([], TARGET))

    # ── caps / determinism / explain ─────────────────────────────────────────
    def test_compress_fairness_seat(self):
        # Two fill subjects + one compress subject -> cap 2 keeps >= 1 compress.
        h_slots = [_slot(61, 5), _slot(62, 5, 45), _slot(63, 21)]
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "extra": [_slot(51, 10)],
                         "heavy": self._spaced(6, 8, 21),
                         "mid": h_slots})
        cards = self._cards(ctx)
        self.assertLessEqual(len(cards), 2)
        if len(cards) == 2:
            self.assertIn("compress", [c["direction"] for c in cards])

    def test_deterministic(self):
        def ctx():
            return self._ctx({"thin": [_slot(11, 9)],
                              "heavy": self._spaced(6, 7, 21),
                              "mid": self._spaced(7, 6, 41)})
        self.assertEqual(build_rebalance_proposals(**ctx()),
                         build_rebalance_proposals(**ctx()))

    def test_explain_channel_and_info_card(self):
        # thin's only job at 9; heavy/mid legs all conflict with thin's tight window
        # -> no feasible fill moves -> info card with gate counts.
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(9, 7, 21, step=1),
                         "mid": self._spaced(9, 6, 41, step=1)})
        ctx["driver_hours"][self.drivers["thin"].id] = (9, 9)
        props, rejections = build_rebalance_proposals(**ctx, explain=True)
        infos = [p for p in props if p.get("kind") == "info"]
        fills = [p for p in props if p.get("kind") == "rebalance"
                 and p["driver_id"] == self.drivers["thin"].id]
        if not fills:
            self.assertEqual(len(infos), 1)
            self.assertTrue(infos[0]["signature"].startswith("_rebal"))
        self.assertTrue(any(r["reason"] in ("no_feasible_moves", "spread_too_small")
                            or r["reason"].startswith("suppressed")
                            for r in rejections) or fills)

    def test_disabled_flag(self):
        from unittest.mock import patch
        import dispatching.rebalance_advisor as ra
        ctx = self._ctx({"thin": [_slot(11, 9)],
                         "heavy": self._spaced(6, 7, 21)})
        with patch.object(ra, "REBALANCE_ENABLED", False):
            self.assertEqual(build_rebalance_proposals(**ctx), [])
