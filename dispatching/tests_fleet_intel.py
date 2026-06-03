"""
Formula + classification-logic tests for the Fleet Capacity Intelligence service
(``dispatching/fleet_intel.py``). These are fast, DB-free unit tests over the PURE economics
and reason-mapping logic — the engine-replay integration path is validated separately by the
``analyze_farmouts`` harness against real boards.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from dispatching import fleet_intel as fi


def fake_leg(**kw):
    """Duck-typed leg exposing only the attributes the pure functions read."""
    d = dict(
        driver_id=1,
        driver=SimpleNamespace(driver_type="affiliate"),
        driver_base_pay=Decimal("90.00"),
        driver_additional=Decimal("10.00"),
        route_id=5,
        route=SimpleNamespace(inhouse_base_pay=Decimal("30.00")),
        revenue_share=Decimal("120.00"),
    )
    d.update(kw)
    return SimpleNamespace(**d)


class FulfillmentTests(SimpleTestCase):
    def test_farm_out(self):
        self.assertEqual(fi.fulfillment_of(fake_leg()), fi.FARM_OUT)

    def test_in_house(self):
        leg = fake_leg(driver=SimpleNamespace(driver_type="inhouse"))
        self.assertEqual(fi.fulfillment_of(leg), fi.IN_HOUSE)

    def test_unassigned(self):
        leg = fake_leg(driver_id=None, driver=None)
        self.assertEqual(fi.fulfillment_of(leg), fi.UNASSIGNED)


class RecoveredMarginTests(SimpleTestCase):
    def test_positive_recoverable(self):
        # affiliate 90 - inhouse 30 = +60 (we'd likely have made more in-house)
        rm = fi.recovered_margin(fake_leg())
        self.assertTrue(rm["available"])
        self.assertEqual(rm["margin"], Decimal("60.00"))
        self.assertEqual(rm["positive"], Decimal("60.00"))
        self.assertEqual(rm["negative"], fi.ZERO)

    def test_negative_validated(self):
        # affiliate 20 - inhouse 30 = -10 (affiliate was cheaper; farm-out validated)
        leg = fake_leg(driver_base_pay=Decimal("20.00"))
        rm = fi.recovered_margin(leg)
        self.assertEqual(rm["margin"], Decimal("-10.00"))
        self.assertEqual(rm["positive"], fi.ZERO)
        self.assertEqual(rm["negative"], Decimal("-10.00"))

    def test_unavailable_when_no_route(self):
        leg = fake_leg(route_id=None, route=None)
        rm = fi.recovered_margin(leg)
        self.assertFalse(rm["available"])
        self.assertIsNone(rm["margin"])
        self.assertEqual(rm["positive"], fi.ZERO)
        self.assertEqual(rm["negative"], fi.ZERO)

    def test_unavailable_when_inhouse_pay_unset(self):
        leg = fake_leg(route=SimpleNamespace(inhouse_base_pay=None))
        self.assertFalse(fi.recovered_margin(leg)["available"])

    def test_unavailable_when_affiliate_cost_blank(self):
        leg = fake_leg(driver_base_pay=None)
        self.assertFalse(fi.recovered_margin(leg)["available"])

    def test_gratuity_additional_excluded_by_default(self):
        # default: driver_additional (10) is NOT added → affiliate base stays 90
        self.assertEqual(fi.affiliate_base_cost(fake_leg()), Decimal("90.00"))

    def test_additional_included_when_flag_set(self):
        with patch.object(fi, "INCLUDE_ADDITIONAL_PAY", True):
            self.assertEqual(fi.affiliate_base_cost(fake_leg()), Decimal("100.00"))
            # margin then = 100 - 30 = 70
            self.assertEqual(fi.recovered_margin(fake_leg())["margin"], Decimal("70.00"))


class RevenueTests(SimpleTestCase):
    def test_uses_stored_revenue_share(self):
        self.assertEqual(fi.leg_revenue(fake_leg()), Decimal("120.00"))

    def test_falls_back_to_calculate(self):
        leg = fake_leg(revenue_share=None)
        leg.calculate_revenue_share = lambda: Decimal("55.00")
        self.assertEqual(fi.leg_revenue(leg), Decimal("55.00"))

    def test_zero_when_uncomputable(self):
        leg = fake_leg(revenue_share=None)
        leg.calculate_revenue_share = lambda: (_ for _ in ()).throw(ValueError())
        self.assertEqual(fi.leg_revenue(leg), fi.ZERO)


class ReasonMappingTests(SimpleTestCase):
    def test_preventable_split(self):
        self.assertTrue(fi.is_preventable(fi.DISPATCH_LEAK))
        self.assertTrue(fi.is_preventable(fi.POSITIONING_ISSUE))
        self.assertTrue(fi.is_preventable(fi.FLIGHT_DELAY_LEAK))
        self.assertFalse(fi.is_preventable(fi.VEHICLE_TYPE_SHORTAGE))
        self.assertFalse(fi.is_preventable(fi.UNIT_CAPACITY_SHORTAGE))
        self.assertFalse(fi.is_preventable(fi.SMART_FARM_OUT))

    def test_families(self):
        self.assertEqual(fi.REASON_FAMILY[fi.VEHICLE_TYPE_SHORTAGE], "capacity")
        self.assertEqual(fi.REASON_FAMILY[fi.UNIT_CAPACITY_SHORTAGE], "capacity")
        self.assertEqual(fi.REASON_FAMILY[fi.DRIVER_IDLE_OR_OFF_SHIFT], "driver")
        self.assertEqual(fi.REASON_FAMILY[fi.DISPATCH_LEAK], "process")
        self.assertEqual(fi.REASON_FAMILY[fi.SMART_FARM_OUT], "strategic")

    def test_reason_category_parsing(self):
        self.assertEqual(fi._reason_category("Outside driver window: clears 23:30 after clear-by 20:00"), "window")
        self.assertEqual(fi._reason_category("Needs 12 more min. Previous job ends ~5:00 PM."), "turnaround")
        self.assertEqual(fi._reason_category("Conflicts with next job at 6:00 PM."), "turnaround")
        self.assertEqual(fi._reason_category("25min buffer"), "other")


class AccumulatorTests(SimpleTestCase):
    def test_acc_splits_margin(self):
        d = {}
        fi._acc(d, "suv", margin=Decimal("60.00"), available=True)
        fi._acc(d, "suv", margin=Decimal("-10.00"), available=True)
        fi._acc(d, "suv", margin=None, available=False)  # uncomputable still counts the leg
        slot = d["suv"]
        self.assertEqual(slot["count"], 3)
        self.assertEqual(slot["available"], 2)
        self.assertEqual(slot["net"], Decimal("50.00"))
        self.assertEqual(slot["positive"], Decimal("60.00"))
        self.assertEqual(slot["negative"], Decimal("-10.00"))

    def test_acc_tracks_paid_and_inhouse(self):
        d = {}
        # 'paid' (spend) accrues even when margin is uncomputable, so 'what we paid' is complete
        fi._acc(d, "Cheapo", margin=Decimal("70.00"), available=True,
                spend=Decimal("100.00"), inhouse=Decimal("30.00"))
        fi._acc(d, "Cheapo", margin=None, available=False, spend=Decimal("50.00"), inhouse=None)
        slot = d["Cheapo"]
        self.assertEqual(slot["spend"], Decimal("150.00"))
        self.assertEqual(slot["inhouse"], Decimal("30.00"))
        self.assertEqual(slot["net"], Decimal("70.00"))
        # margin % = net / spend = 70 / 150 ≈ 46.7%
        self.assertAlmostEqual(float(slot["net"]) / float(slot["spend"]) * 100, 46.666, places=1)


class ActionMappingTests(SimpleTestCase):
    def test_reason_to_action(self):
        self.assertEqual(fi.REASON_ACTION[fi.DISPATCH_LEAK], fi.ACT_PREVENTABLE)
        self.assertEqual(fi.REASON_ACTION[fi.UNIT_CAPACITY_SHORTAGE], fi.ACT_HIRE)
        self.assertEqual(fi.REASON_ACTION[fi.DRIVER_IDLE_OR_OFF_SHIFT], fi.ACT_HIRE)
        self.assertEqual(fi.REASON_ACTION[fi.FLIGHT_DELAY_LEAK], fi.ACT_DELAY)
        self.assertEqual(fi.REASON_ACTION[fi.VEHICLE_TYPE_SHORTAGE], fi.ACT_BUY)
        self.assertEqual(fi.REASON_ACTION[fi.POSITIONING_ISSUE], fi.ACT_POSITION)

    def test_preventable_is_first_in_order(self):
        self.assertEqual(fi.ACTION_ORDER[0], fi.ACT_PREVENTABLE)
