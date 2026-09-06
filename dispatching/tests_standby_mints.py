"""Standby-mint core + handoff-chain band tests (scheduling redesign, Build 2).

The engine itself is verified byte-identical against analysis/10 (the
extraction gate) and against the raw replay state on ten dates
(analysis/13_build2_gate.py); these tests pin the pure-core behaviors a
refactor could silently lose: the pool rule, the strict-share geometry, the
≤2 rule, OOS/rest gating, D6 soft packing, and the 03 §3.2 band rule with its
volatility guard.
"""
from datetime import date, datetime, timedelta

from django.test import SimpleTestCase

from dispatching import handoff_chain as hc
from dispatching import standby_mints as sm

DAY = date(2026, 6, 10)


def L(lid, hour, minute=0, kind="OTHER", did=None, tier=0):
    return sm.MintLeg(lid, DAY, datetime(2026, 6, 10, hour, minute), kind, did, tier)


def run(boards, farmed, dva, fleet, standby, **kw):
    kw.setdefault("gap", 120)
    kw.setdefault("buf", 30)
    return sm.replay_one_day(DAY, boards, farmed, dva, fleet, standby, **kw)


class StandbyPoolRuleTests(SimpleTestCase):
    def test_adopted_rule(self):
        # Active in-house candidates minus worked / rostered / off — nothing else.
        pool = sm.standby_pool_ids([1, 2, 3, 4, 5], works_today={2},
                                   dva_today={3: 9}, off_today={4})
        self.assertEqual(pool, [1, 5])


class MintEngineTests(SimpleTestCase):
    def setUp(self):
        self.fleet = {9: {"active": True, "tier": 2}}
        self.dva = {10: 9}                       # driver 10 holds unit 9
        # Early start: an evening add would push the holder past 13.5h, so the
        # waterfall cannot simply refill him — the mint lever is what's tested.
        self.boards = {10: [L(1, 5), L(2, 10)]}

    def test_evening_farm_leg_mints_on_the_late_side(self):
        farmed = [L(50, 18, did=999)]
        r = run(self.boards, farmed, self.dva, self.fleet, [77])
        self.assertEqual(len(r["mints"]), 1)
        m = r["mints"][0]
        self.assertEqual((m["driver"], m["veh"], m["side"]), (77, 9, "late"))
        self.assertEqual(r["refill_farm"], 1)

    def test_gap_rule_blocks_a_tight_mint(self):
        # 11:30 farmed pickup: 90 min after the holder's 10:00 — under gap 120,
        # and too tight to buffer onto his own board. Stays residual; no mint.
        farmed = [L(50, 11, 30, did=999)]
        r = run(self.boards, farmed, self.dva, self.fleet, [77])
        self.assertEqual(len(r["mints"]), 0)
        self.assertEqual(r["roster_refill"], 0)
        self.assertIn("no_car_side", r["fail_reasons"])

    def test_oos_car_never_minted(self):
        farmed = [L(50, 18, did=999)]
        r = run(self.boards, farmed, self.dva, self.fleet, [77],
                is_oos=lambda v: v == 9)
        self.assertEqual(len(r["mints"]), 0)
        self.assertIn("no_car_side", r["fail_reasons"])

    def test_two_roster_drivers_take_no_mint(self):
        # Unit 9 already carries an AM/PM pair; a 20:30 leg can neither buffer
        # onto either board nor mint a THIRD driver onto the car (≤2 rule).
        dva = {10: 9, 11: 9}
        boards = {10: [L(1, 5)], 11: [L(2, 20)]}
        farmed = [L(50, 20, 30, did=999)]
        r = run(boards, farmed, dva, self.fleet, [77])
        self.assertEqual(len(r["mints"]), 0)
        self.assertEqual(r["refill_farm"], 0)

    def test_rest_callback_blocks_the_body(self):
        farmed = [L(50, 18, did=999)]
        r = run(self.boards, farmed, self.dva, self.fleet, [77],
                rest_ok_first=lambda did, d, t: did != 77)
        self.assertEqual(len(r["mints"]), 0)
        self.assertIn("no_standby_body", r["fail_reasons"])

    def test_co_driver_overlap_banned(self):
        # A farmed leg inside the holder's occupancy can neither join his board
        # (buffer) nor mint on his car (overlap + gap) — it stays residual.
        farmed = [L(50, 10, 30, did=999)]
        r = run(self.boards, farmed, self.dva, self.fleet, [77])
        self.assertEqual(len(r["mints"]), 0)
        self.assertEqual(r["refill_farm"], 0)

    def test_cap_shed_then_mint_recaptures(self):
        # A 16h day sheds its far edge; the shed evening leg lands on a mint.
        boards = {10: [L(1, 6), L(2, 8), L(3, 22)]}
        farmed = []
        r = run(boards, farmed, self.dva, self.fleet, [77])
        self.assertEqual(r["capped_days"], 1)
        self.assertEqual(r["shed"], 1)
        self.assertEqual(len(r["mints"]), 1)
        self.assertEqual(r["mints"][0]["legs"][0].id, 3)
        self.assertLessEqual(sm.span_h(r["boards"][10]), sm.SPAN_CAP_H)

    def test_soft_policy_prefers_a_site_that_can_pack_two(self):
        # Two cars free-side; unit 8 can capture BOTH evening legs, unit 9 only
        # the first (tier). Soft packing must seed the mint on unit 8.
        fleet = {8: {"active": True, "tier": 2}, 9: {"active": True, "tier": 0}}
        dva = {10: 8, 11: 9}
        boards = {10: [L(1, 5)], 11: [L(2, 5, 30)]}
        farmed = [L(50, 18, did=999, tier=0), L(51, 20, 30, did=999, tier=2)]
        r = run(boards, farmed, dva, fleet, [77], policy="soft")
        self.assertEqual(len(r["mints"]), 1)
        self.assertEqual(r["mints"][0]["veh"], 8)
        self.assertEqual([l.id for l in r["mints"][0]["legs"]], [50, 51])


class HandoffBandTests(SimpleTestCase):
    # MCO -> MCO: central 83.0, low 79.0 (fuel closed at 8), skip-wash floor 34.
    def test_chain_component_arithmetic(self):
        lo, ce, hi = hc.clear_to_pickup_min("MCO Terminal", "MCO Terminal")
        self.assertEqual((lo, ce, hi), (79.0, 83.0, 87.0))
        self.assertEqual(hc.skip_wash_floor_min("MCO Terminal", "MCO Terminal"), 34.0)

    def test_green_amber_red(self):
        g = hc.handoff_band("MCO Terminal", "MCO Terminal", 200)
        self.assertEqual(g["band"], "green")
        a = hc.handoff_band("MCO Terminal", "MCO Terminal", 80)
        self.assertEqual(a["band"], "amber")
        fast = hc.handoff_band("MCO Terminal", "MCO Terminal", 40)
        self.assertEqual(fast["band"], "amber")     # skip-wash fast path
        r = hc.handoff_band("MCO Terminal", "MCO Terminal", 20)
        self.assertEqual(r["band"], "red")

    def test_volatility_guard_on_arrivals(self):
        # 90 min clears central (83) for a non-arrival, but NOT central + the
        # 13-min P75 retime when the incoming first job is a flight arrival.
        plain = hc.handoff_band("MCO Terminal", "MCO Terminal", 90)
        self.assertEqual(plain["band"], "green")
        arr = hc.handoff_band("MCO Terminal", "MCO Terminal", 90,
                              incoming_is_arrival=True)
        self.assertEqual(arr["band"], "amber")
        arr_ok = hc.handoff_band("MCO Terminal", "MCO Terminal", 97,
                                 incoming_is_arrival=True)
        self.assertEqual(arr_ok["band"], "green")

    def test_pct_scalers(self):
        # green_pct 50 halves the central bar; amber_floor_pct 50 halves RED's
        # onset (Port→SFB: low 177, skip-wash 137 — 100 min is red at 100%,
        # amber once the floor is halved to 88.5).
        g = hc.handoff_band("MCO Terminal", "MCO Terminal", 45, green_pct=50)
        self.assertEqual(g["band"], "green")
        base = hc.handoff_band("Port Canaveral Area", "SFB Terminal", 100)
        self.assertEqual(base["band"], "red")
        r = hc.handoff_band("Port Canaveral Area", "SFB Terminal", 100,
                            amber_floor_pct=50)
        self.assertEqual(r["band"], "amber")

    def test_unknown_zone_falls_back_to_other(self):
        b = hc.handoff_band("Nowhere Special", "Also Nowhere", 500)
        self.assertEqual(b["band"], "green")

    def test_car_ready_uses_founder_chain(self):
        # MCO drop: 15.5 + 17.5 + 8 + 20 = 61 central minutes to base.
        self.assertEqual(hc.car_ready_min("MCO Terminal")[1], 61.0)
