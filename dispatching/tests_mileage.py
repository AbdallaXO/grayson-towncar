"""
Tests for dispatching/mileage.py — the OBD -> GPS mileage resolver.

These run before the module is wired to anything. The arithmetic here decides
when preventive maintenance fires and what every mileage figure on the Fleet
pages says, so it is proven against fixtures first.

No DB, no HTTP, no clock — SimpleTestCase throughout.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from dispatching.mileage import (
    MAX_PLAUSIBLE_DAY_METERS,
    METERS_PER_MILE,
    SOURCE_GPS,
    SOURCE_NONE,
    SOURCE_OBD,
    MileageResult,
    OdometerReading,
    meters_to_miles,
    resolve_day_mileage,
)

VID = "281475002537740"
OTHER_VID = "281475002537741"


def miles(n):
    """Miles -> meters, for writing readable fixtures."""
    return Decimal(str(n)) * METERS_PER_MILE


def reading(vid=VID, obd=None, gps=None):
    return OdometerReading(
        samsara_vehicle_id=vid, obd_odometer_meters=obd, gps_distance_meters=gps
    )


class MetersToMilesTests(SimpleTestCase):
    def test_converts_and_rounds(self):
        self.assertEqual(meters_to_miles(METERS_PER_MILE), Decimal("1.0"))
        self.assertEqual(meters_to_miles(METERS_PER_MILE * 100), Decimal("100.0"))

    def test_none_passes_through_and_is_not_zero(self):
        self.assertIsNone(meters_to_miles(None))

    def test_honours_places(self):
        self.assertEqual(meters_to_miles(METERS_PER_MILE, places=2), Decimal("1.00"))

    def test_zero_is_zero_not_none(self):
        self.assertEqual(meters_to_miles(0), Decimal("0.0"))


class ObdPreferredTests(SimpleTestCase):
    def test_uses_obd_when_both_ends_have_it(self):
        result = resolve_day_mileage(
            reading(obd=miles(50_000)), reading(obd=miles(50_120))
        )
        self.assertEqual(result.source, SOURCE_OBD)
        self.assertEqual(result.miles, Decimal("120.0"))
        self.assertEqual(result.note, "")

    def test_prefers_obd_even_when_gps_also_present(self):
        # GPS chords corners and under-reads; OBD is the real clock.
        result = resolve_day_mileage(
            reading(obd=miles(10_000), gps=miles(2_000)),
            reading(obd=miles(10_100), gps=miles(2_095)),
        )
        self.assertEqual(result.source, SOURCE_OBD)
        self.assertEqual(result.miles, Decimal("100.0"))

    def test_stationary_day_is_zero_not_unknown(self):
        # Equal readings mean the car provably did not move. That is a real 0,
        # and it must be distinguishable from "we have no idea".
        result = resolve_day_mileage(
            reading(obd=miles(50_000)), reading(obd=miles(50_000))
        )
        self.assertEqual(result.meters, Decimal("0"))
        self.assertEqual(result.source, SOURCE_OBD)
        self.assertTrue(result.is_known)

    def test_gps_odometer_cannot_be_supplied_at_all(self):
        # gpsOdometerMeters is rejected structurally: there is no field for it.
        # If someone adds one later this test fails and forces the conversation.
        self.assertNotIn(
            "gps_odometer_meters", OdometerReading.__dataclass_fields__
        )


class NeverNegativeTests(SimpleTestCase):
    def test_backwards_obd_falls_back_to_gps(self):
        # ECU reset / bad frame. Under-report via GPS rather than fabricate a
        # rollover — wrong in the safe direction.
        result = resolve_day_mileage(
            reading(obd=miles(50_000), gps=miles(1_000)),
            reading(obd=miles(12), gps=miles(1_080)),
        )
        self.assertEqual(result.source, SOURCE_GPS)
        self.assertEqual(result.miles, Decimal("80.0"))
        self.assertIn("backwards", result.note)

    def test_backwards_obd_with_no_gps_is_unknown_not_negative(self):
        result = resolve_day_mileage(
            reading(obd=miles(50_000)), reading(obd=miles(49_000))
        )
        self.assertIsNone(result.meters)
        self.assertEqual(result.source, SOURCE_NONE)
        self.assertIn("backwards", result.note)

    def test_backwards_gps_counter_is_unknown(self):
        # A cumulative GPS counter that drops has reset (gateway replaced). The
        # day is unknowable — emphatically not zero.
        result = resolve_day_mileage(
            reading(gps=miles(5_000)), reading(gps=miles(20))
        )
        self.assertIsNone(result.meters)
        self.assertEqual(result.source, SOURCE_NONE)
        self.assertIn("reset", result.note)

    def test_no_path_ever_returns_a_negative(self):
        # Exhaustive-ish sweep over adversarial combinations.
        values = [None, 0, miles(1), miles(500), miles(50_000), miles(1_000_000)]
        for p_obd in values:
            for c_obd in values:
                for p_gps in values:
                    for c_gps in values:
                        result = resolve_day_mileage(
                            reading(obd=p_obd, gps=p_gps),
                            reading(obd=c_obd, gps=c_gps),
                        )
                        if result.meters is not None:
                            self.assertGreaterEqual(
                                result.meters, 0,
                                f"negative from {p_obd},{c_obd},{p_gps},{c_gps}",
                            )


class ImplausibleStepTests(SimpleTestCase):
    def test_absurd_obd_jump_falls_back_to_gps(self):
        result = resolve_day_mileage(
            reading(obd=miles(50_000), gps=miles(1_000)),
            reading(obd=miles(950_000), gps=miles(1_060)),
        )
        self.assertEqual(result.source, SOURCE_GPS)
        self.assertEqual(result.miles, Decimal("60.0"))
        self.assertIn("ceiling", result.note)

    def test_absurd_obd_jump_with_no_gps_is_unknown(self):
        result = resolve_day_mileage(
            reading(obd=miles(50_000)), reading(obd=miles(950_000))
        )
        self.assertIsNone(result.meters)
        self.assertIn("ceiling", result.note)

    def test_absurd_gps_delta_is_rejected(self):
        result = resolve_day_mileage(
            reading(gps=miles(1_000)), reading(gps=miles(90_000))
        )
        self.assertIsNone(result.meters)
        self.assertEqual(result.source, SOURCE_NONE)

    def test_value_exactly_at_ceiling_is_accepted(self):
        result = resolve_day_mileage(
            reading(obd=Decimal("0") + 1),
            reading(obd=Decimal("1") + MAX_PLAUSIBLE_DAY_METERS),
        )
        self.assertEqual(result.source, SOURCE_OBD)
        self.assertEqual(result.meters, MAX_PLAUSIBLE_DAY_METERS)

    def test_a_hard_but_real_double_still_passes(self):
        # MCO -> Miami and back is ~470 mi. The ceiling must not clip real work.
        result = resolve_day_mileage(
            reading(obd=miles(80_000)), reading(obd=miles(80_470))
        )
        self.assertEqual(result.source, SOURCE_OBD)
        self.assertEqual(result.miles, Decimal("470.0"))


class GatewaySwapTests(SimpleTestCase):
    def test_refuses_to_diff_across_different_samsara_ids(self):
        # THE destructive case: one gateway moved between cars. Diffing here
        # invents a six-figure day that poisons every rollup above it.
        result = resolve_day_mileage(
            reading(vid=VID, obd=miles(50_000)),
            reading(vid=OTHER_VID, obd=miles(120_000)),
        )
        self.assertIsNone(result.meters)
        self.assertEqual(result.source, SOURCE_NONE)
        self.assertIn("gateway changed", result.note)

    def test_refusal_wins_even_when_the_delta_looks_perfectly_normal(self):
        # A plausible-looking delta across two gateways is the dangerous one --
        # it would sail through every other guard.
        result = resolve_day_mileage(
            reading(vid=VID, obd=miles(50_000)),
            reading(vid=OTHER_VID, obd=miles(50_100)),
        )
        self.assertIsNone(result.meters)
        self.assertIn("gateway changed", result.note)


class MissingDataTests(SimpleTestCase):
    def test_no_previous_reading_is_unknown(self):
        result = resolve_day_mileage(None, reading(obd=miles(50_000)))
        self.assertIsNone(result.meters)
        self.assertEqual(result.source, SOURCE_NONE)
        self.assertFalse(result.is_known)

    def test_no_current_reading_is_unknown(self):
        self.assertIsNone(resolve_day_mileage(reading(obd=miles(1)), None).meters)

    def test_gps_only_vehicle_uses_gps(self):
        # An AG-series asset gateway supplies no OBD at all.
        result = resolve_day_mileage(
            reading(gps=miles(3_000)), reading(gps=miles(3_045))
        )
        self.assertEqual(result.source, SOURCE_GPS)
        self.assertEqual(result.miles, Decimal("45.0"))

    def test_obd_on_only_one_end_falls_back_to_gps(self):
        result = resolve_day_mileage(
            reading(obd=None, gps=miles(100)),
            reading(obd=miles(50_000), gps=miles(130)),
        )
        self.assertEqual(result.source, SOURCE_GPS)
        self.assertEqual(result.miles, Decimal("30.0"))

    def test_nothing_usable_is_unknown(self):
        result = resolve_day_mileage(reading(), reading())
        self.assertIsNone(result.meters)
        self.assertEqual(result.source, SOURCE_NONE)

    def test_zero_obd_odometer_is_treated_as_missing(self):
        # No in-service car reads 0 on the clock; that is a gateway reporting
        # nothing. Fall through to GPS rather than invent a 50,000-mile day.
        result = resolve_day_mileage(
            reading(obd=Decimal("0"), gps=miles(200)),
            reading(obd=miles(50_000), gps=miles(240)),
        )
        self.assertEqual(result.source, SOURCE_GPS)
        self.assertEqual(result.miles, Decimal("40.0"))

    def test_zero_gps_distance_is_legitimate(self):
        # GPS distance is a counter, not an odometer — starting at 0 is normal.
        result = resolve_day_mileage(
            reading(gps=Decimal("0")), reading(gps=miles(25))
        )
        self.assertEqual(result.source, SOURCE_GPS)
        self.assertEqual(result.miles, Decimal("25.0"))


class GarbageInputTests(SimpleTestCase):
    def test_non_numeric_values_do_not_raise(self):
        for junk in ["", "n/a", object(), [], {}]:
            result = resolve_day_mileage(
                reading(obd=junk, gps=junk), reading(obd=junk, gps=junk)
            )
            self.assertIsNone(result.meters)
            self.assertEqual(result.source, SOURCE_NONE)

    def test_negative_raw_counter_is_discarded_not_used(self):
        result = resolve_day_mileage(
            reading(obd=Decimal("-5"), gps=miles(10)),
            reading(obd=miles(50_000), gps=miles(35)),
        )
        self.assertEqual(result.source, SOURCE_GPS)
        self.assertEqual(result.miles, Decimal("25.0"))

    def test_numeric_strings_from_json_are_accepted(self):
        # Samsara sends ints, but a JSON round-trip can stringify them.
        # 80467200 m = 50,000 mi; +160934 m = +100 mi.
        result = resolve_day_mileage(
            reading(obd="80467200"), reading(obd="80628134")
        )
        self.assertEqual(result.source, SOURCE_OBD)
        self.assertEqual(result.miles, Decimal("100.0"))


class ResultContractTests(SimpleTestCase):
    def test_unknown_is_none_never_zero(self):
        # The invariant the whole UI depends on. If this ever regresses, a dead
        # gateway starts looking like a parked car.
        result = resolve_day_mileage(None, None)
        self.assertIsNone(result.meters)
        self.assertIsNone(result.miles)
        self.assertNotEqual(result.meters, 0)

    def test_result_is_immutable(self):
        result = MileageResult(Decimal("100"), SOURCE_OBD)
        with self.assertRaises(Exception):
            result.meters = Decimal("200")

    def test_source_is_always_one_of_the_three(self):
        for prev, cur in [
            (None, None),
            (reading(obd=miles(1)), reading(obd=miles(2))),
            (reading(gps=miles(1)), reading(gps=miles(2))),
            (reading(vid=VID), reading(vid=OTHER_VID)),
        ]:
            self.assertIn(
                resolve_day_mileage(prev, cur).source,
                {SOURCE_OBD, SOURCE_GPS, SOURCE_NONE},
            )
