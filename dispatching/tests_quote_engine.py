"""Tests for the dispatcher quote engine.

Three jobs:

1. Prove the PUBLISHED RATE CARD always wins. This is the whole reason the
   engine exists — before it, the formula quoted MCO -> Disney at $135 while the
   website sold the identical transfer at $105.
2. Pin the founder-confirmed calibration anchors so a future rate edit that
   moves them fails loudly.
3. Cover the parsing/rounding/matching bugs found in the 2026-07-29 audit, so
   they cannot come back.
"""

import json
from decimal import Decimal
from pathlib import Path
from unittest import SkipTest
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from dispatching import quote_engine as qe
from rates.models import Location, Rate, Route, Vehicle

User = get_user_model()


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers — no database
# ════════════════════════════════════════════════════════════════════════════


class ParseDistanceTests(TestCase):
    def test_plain_miles(self):
        self.assertEqual(qe.parse_distance_miles("45.2 mi"), Decimal("45.200"))

    def test_thousands_separator(self):
        self.assertEqual(qe.parse_distance_miles("1,234 mi"), Decimal("1234.000"))

    def test_feet_are_not_miles(self):
        """Google returns feet under ~0.1 mi. The old parser read '285 ft' as
        285 MILES, quoting ~$1,010 for a trip across a parking lot."""
        miles = qe.parse_distance_miles("285 ft")
        self.assertLess(miles, Decimal("0.1"))
        self.assertGreater(miles, Decimal("0"))

    def test_feet_quote_stays_at_minimum(self):
        miles = qe.parse_distance_miles("285 ft")
        price, _bd, _notes = qe.formula_oneway("towncar", miles)
        self.assertEqual(price, Decimal("135"))

    def test_kilometers(self):
        self.assertEqual(
            qe.parse_distance_miles("10 km").quantize(Decimal("0.01")),
            Decimal("6.21"),
        )

    def test_bare_number_assumed_miles(self):
        self.assertEqual(qe.parse_distance_miles("18"), Decimal("18.000"))

    def test_unparseable(self):
        for bad in (None, "", "   ", "not a distance"):
            self.assertIsNone(qe.parse_distance_miles(bad))


class RoundToFiveTests(TestCase):
    def test_halves_round_up_consistently(self):
        """Python's round() is banker's rounding on Decimals: $132.50 went DOWN
        to $130 while $127.50 went UP to $130. A dispatcher checking by hand
        disagreed with the tool half the time."""
        cases = {
            "127.50": "130",
            "132.50": "135",
            "137.50": "140",
            "142.50": "145",
            "2.50": "5",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(qe.round_to_5(Decimal(raw)), Decimal(expected))

    def test_exact_multiples_unchanged(self):
        for value in ("135", "840", "0"):
            self.assertEqual(qe.round_to_5(Decimal(value)), Decimal(value))


class DirectionTests(TestCase):
    def test_local_trips_have_no_direction(self):
        self.assertEqual(
            qe.classify_direction(Decimal("40"), Decimal("5")), qe.DIRECTION_UNKNOWN
        )

    def test_pickup_far_from_base_is_inbound(self):
        self.assertEqual(
            qe.classify_direction(Decimal("235"), Decimal("230")),
            qe.DIRECTION_INBOUND,
        )

    def test_pickup_near_base_is_outbound(self):
        self.assertEqual(
            qe.classify_direction(Decimal("235"), Decimal("12")),
            qe.DIRECTION_OUTBOUND,
        )

    def test_unknown_positioning_falls_back_to_symmetric(self):
        self.assertEqual(
            qe.classify_direction(Decimal("235"), None), qe.DIRECTION_UNKNOWN
        )

    def test_direction_does_not_change_the_price(self):
        """An outbound discount was added from Blacklane data and removed the
        same day — that asymmetry is a network property. An Orlando fleet eats
        the empty return whichever way the revenue leg runs, and the discount
        was cutting 17.5% off exactly the trips the founder prices highest."""
        out, _b1, _n1 = qe.formula_oneway(
            "towncar", Decimal("218"), 194, qe.DIRECTION_OUTBOUND
        )
        inb, _b2, _n2 = qe.formula_oneway(
            "towncar", Decimal("218"), 194, qe.DIRECTION_INBOUND
        )
        unknown, _b3, _n3 = qe.formula_oneway("towncar", Decimal("218"), 194)
        self.assertEqual(out, inb)
        self.assertEqual(out, unknown)


class UnknownVehicleTests(TestCase):
    def test_unknown_vehicle_raises_instead_of_pricing_as_towncar(self):
        """The old view did QUOTE_FORMULA.get(vt, towncar) — adding a vehicle
        type in the admin silently quoted it at towncar prices."""
        with self.assertRaises(KeyError):
            qe.formula_oneway("stretch_limo", Decimal("20"))


# ════════════════════════════════════════════════════════════════════════════
# Founder-confirmed calibration anchors (2026-07-29)
# ════════════════════════════════════════════════════════════════════════════


class CalibrationAnchorTests(TestCase):
    """If a rate edit moves one of these, that is a business decision and this
    test should fail until the anchor is re-confirmed."""

    def test_port_everglades_matches_the_founders_stated_minimums(self):
        """Disney -> Port Everglades, 218 mi / 3h14m. Founder, 2026-07-29:
        towncar minimum $850, SUV minimum $920, Sprinter $1,400 — each a FARE,
        with 20% gratuity quoted on top."""
        for vehicle_type, expected in (
            ("towncar", "850"), ("suv", "920"), ("Van(14 Pax)", "1400"),
        ):
            with self.subTest(vehicle=vehicle_type):
                price, _bd, _n = qe.formula_oneway(
                    vehicle_type, Decimal("218"), minutes=194
                )
                self.assertEqual(price, Decimal(expected))

    def test_vehicle_prices_stay_in_order_on_a_long_haul(self):
        prices = [
            qe.formula_oneway(vt, Decimal("218"), 194)[0]
            for vt in qe.VEHICLE_TIER_ORDER
        ]
        self.assertEqual(prices, sorted(prices))

    def test_tampa_outbound_towncar_is_340(self):
        # 85 mi is below the long-distance threshold, so no directional change.
        price, _bd, _notes = qe.formula_oneway(
            "towncar", Decimal("85"), minutes=78, direction=qe.DIRECTION_OUTBOUND
        )
        self.assertEqual(price, Decimal("340"))

    def test_short_custom_trip_holds_the_dispatch_minimum(self):
        for miles in ("2", "6", "11", "13"):
            with self.subTest(miles=miles):
                price, breakdown, _n = qe.formula_oneway("towncar", Decimal(miles))
                self.assertEqual(price, Decimal("135"))
                self.assertTrue(breakdown["minimum_applied"])

    def test_long_haul_rate_only_applies_past_the_threshold(self):
        """Tampa (85 mi) must keep the original rate; the long-haul rate starts
        past 100 mi so short and mid-range trips are untouched."""
        rates = qe.VEHICLE_RATES["towncar"]
        self.assertEqual(
            qe.mileage_charge(rates, Decimal("85")), rates.per_mile * Decimal("85")
        )
        at_150 = qe.mileage_charge(rates, Decimal("150"))
        self.assertEqual(
            at_150,
            rates.per_mile * Decimal("100") + rates.long_per_mile * Decimal("50"),
        )
        self.assertGreater(at_150, rates.per_mile * Decimal("150"))

    def test_time_floor_binds_only_on_slow_routes(self):
        """45 mi in 61 min (LEGOLAND) is slow for its distance — the hourly
        floor should take over. 85 mi in 78 min (Tampa) is not."""
        slow, slow_bd, _n1 = qe.formula_oneway("towncar", Decimal("45"), minutes=61)
        fast, fast_bd, _n2 = qe.formula_oneway("towncar", Decimal("85"), minutes=78)
        self.assertTrue(slow_bd["time_floor_applied"])
        self.assertFalse(fast_bd["time_floor_applied"])
        self.assertGreater(slow, Decimal("205"))  # above pure mileage
        self.assertEqual(fast, Decimal("340"))

    def test_breakdown_exposes_the_empty_return_split(self):
        """The guest sees one number; the dispatcher must be able to explain it."""
        _price, breakdown, _notes = qe.formula_oneway("towncar", Decimal("235"))
        # The shares must sum to the mileage total exactly, or the on-screen
        # breakdown does not add up. An odd-penny total splits 393.63/393.62.
        self.assertEqual(
            breakdown["revenue_leg_share"] + breakdown["empty_return_share"],
            breakdown["mileage_fee"],
        )
        self.assertLessEqual(
            abs(breakdown["revenue_leg_share"] - breakdown["empty_return_share"]),
            Decimal("0.01"),
        )
        self.assertEqual(breakdown["per_driven_mile"], Decimal("1.68"))

    def test_every_vehicle_tier_is_priced_and_ordered(self):
        prices = []
        for vehicle_type in qe.VEHICLE_TIER_ORDER:
            price, _bd, _n = qe.formula_oneway(vehicle_type, Decimal("100"))
            prices.append(price)
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(len(prices), 5)


# ════════════════════════════════════════════════════════════════════════════
# Rate-card matching and precedence (database)
# ════════════════════════════════════════════════════════════════════════════


class RateCardFixtureMixin:
    """The real published card, as shipped in rates_data.json."""

    CARD = {
        ("Orlando International Airport", "All WDW Disney Property Resorts"): {
            "towncar": ("105.00", "195.00"),
            "mini_van": ("120.00", "230.00"),
            "suv": ("140.00", "275.00"),
            "van": ("160.00", "305.00"),
        },
        ("Orlando International Airport", "Universal Studios Area Hotels"): {
            "towncar": ("105.00", "195.00"),
            "suv": ("140.00", "275.00"),
        },
        ("Orlando International Airport", "International Drive Hotels"): {
            "towncar": ("105.00", "195.00"),
        },
        ("Orlando International Airport", "Kissimmee 192 Area Hotels"): {
            "towncar": ("115.00", "220.00"),
        },
        ("Orlando International Airport", "Omni Championsgate / Reunion"): {
            "towncar": ("130.00", "250.00"),
        },
        ("All WDW Disney Property Resorts", "Universal Studios Area Hotels"): {
            "towncar": ("85.00", "170.00"),
        },
        ("Sea World", "All WDW Disney Property Resorts"): {
            "towncar": ("75.00", "140.00"),
        },
        ("All WDW Disney Property Resorts", "Port Canaveral"): {
            "towncar": ("185.00", "360.00"),
            "van": ("255.00", "495.00"),
        },
        ("Port Canaveral", "Orlando International Airport"): {
            "towncar": ("160.00", "315.00"),
        },
        ("Sanford Int'l Airport", "All WDW Disney Property Resorts"): {
            "towncar": ("165.00", "315.00"),
        },
    }

    def build_card(self):
        self.vehicles = {}
        for vehicle_type, capacity in (
            ("towncar", 4), ("mini_van", 5), ("suv", 6), ("van", 10),
        ):
            self.vehicles[vehicle_type] = Vehicle.objects.create(
                vehicle_type=vehicle_type, capacity=capacity, luggage_capacity=capacity
            )
        self.locations = {}
        for (origin, destination) in self.CARD:
            for name in (origin, destination):
                if name not in self.locations:
                    self.locations[name] = Location.objects.create(name=name)
        self.routes = {}
        for pair, vehicle_prices in self.CARD.items():
            route = Route.objects.create(
                origin=self.locations[pair[0]], destination=self.locations[pair[1]]
            )
            self.routes[pair] = route
            for vehicle_type, (oneway, roundtrip) in vehicle_prices.items():
                Rate.objects.create(
                    vehicle=self.vehicles[vehicle_type],
                    route=route,
                    oneway_price=Decimal(oneway),
                    round_trip_price=Decimal(roundtrip),
                )


class LocationMatchingTests(RateCardFixtureMixin, TestCase):
    def setUp(self):
        self.build_card()

    def _all(self):
        return list(Location.objects.all())

    def test_matches_a_real_typed_address(self):
        """Seeded aliases must cover what dispatchers actually type. The card's
        Location rows are broad categories and their alias fields ship empty."""
        cases = {
            "Orlando International Airport, Orlando, FL": "Orlando International Airport",
            "MCO": "Orlando International Airport",
            "Disney's Grand Floridian Resort & Spa, 4401 Floridian Way": "All WDW Disney Property Resorts",
            "Walt Disney World Swan and Dolphin Resort": "All WDW Disney Property Resorts",
            "Loews Portofino Bay Hotel, 5601 Universal Blvd": "Universal Studios Area Hotels",
            "Rosen Centre Hotel, 9840 International Dr": "International Drive Hotels",
            "SeaWorld Orlando": "Sea World",
            "Cruise Terminal 8, Port Canaveral, FL": "Port Canaveral",
            "Omni Orlando Resort at ChampionsGate, 1500 Masters Blvd": "Omni Championsgate / Reunion",
            "Orlando Sanford International Airport": "Sanford Int'l Airport",
        }
        for address, expected in cases.items():
            with self.subTest(address=address):
                location, keyword = qe.match_location(address, self._all())
                self.assertIsNotNone(location, f"no match for {address!r}")
                self.assertEqual(location.name, expected, f"keyword was {keyword!r}")

    def test_longest_keyword_wins_not_last_row(self):
        """The old loop kept overwriting, so the LAST matching row won and the
        detected route depended on database ordering."""
        generic = Location.objects.create(name="Orlando", aliases="Orlando")
        self.addCleanup(generic.delete)
        location, keyword = qe.match_location(
            "Orlando International Airport, FL", self._all()
        )
        self.assertEqual(location.name, "Orlando International Airport")
        self.assertGreater(len(keyword), len("Orlando"))

    def test_short_aliases_require_word_boundaries(self):
        """'MCO' must not match inside an unrelated word."""
        location, _kw = qe.match_location("123 Mcormick Lane, Ocala FL", self._all())
        self.assertNotEqual(
            getattr(location, "name", None), "Orlando International Airport"
        )

    def test_unknown_address_matches_nothing(self):
        location, _kw = qe.match_location(
            "742 Evergreen Terrace, Springfield", self._all()
        )
        self.assertIsNone(location)

    def test_matching_is_stable_regardless_of_row_order(self):
        address = "Disney's Grand Floridian Resort, Lake Buena Vista FL"
        forward, _k1 = qe.match_location(address, self._all())
        backward, _k2 = qe.match_location(address, list(reversed(self._all())))
        self.assertEqual(forward.pk, backward.pk)


class RateCardPrecedenceTests(RateCardFixtureMixin, TestCase):
    """Every published price must come back verbatim, for both trip types and
    both directions of travel."""

    def setUp(self):
        self.build_card()

    def test_every_published_price_is_returned_verbatim(self):
        checked = 0
        for (origin, destination), vehicle_prices in self.CARD.items():
            for vehicle_type, (oneway, roundtrip) in vehicle_prices.items():
                for pickup, dropoff in (
                    (origin, destination),
                    (destination, origin),  # card routes are bidirectional
                ):
                    for trip_type, expected in (
                        ("oneway", oneway), ("roundtrip", roundtrip),
                    ):
                        with self.subTest(
                            route=f"{pickup}->{dropoff}",
                            vehicle=vehicle_type,
                            trip=trip_type,
                        ):
                            result = qe.quote(
                                vehicle_type=vehicle_type,
                                trip_type=trip_type,
                                miles=Decimal("20"),
                                minutes=28,
                                pickup_location=self.locations[pickup],
                                dropoff_location=self.locations[dropoff],
                            )
                            self.assertTrue(result.is_rate_card)
                            self.assertEqual(result.price, Decimal(expected))
                            checked += 1
        # 4+2+1+1+1+1+1+2+1+1 = 15 vehicle/route pairs x 2 directions x 2 trip types
        self.assertEqual(checked, 60)

    def test_card_beats_the_formula_where_they_disagree(self):
        """MCO -> Disney: the card sells $105, the formula wanted $135."""
        formula_price, _bd, _n = qe.formula_oneway("towncar", Decimal("20"), minutes=28)
        self.assertEqual(formula_price, Decimal("135"))
        result = qe.quote(
            vehicle_type="towncar",
            miles=Decimal("20"),
            minutes=28,
            pickup_location=self.locations["Orlando International Airport"],
            dropoff_location=self.locations["All WDW Disney Property Resorts"],
        )
        self.assertEqual(result.price, Decimal("105"))
        self.assertTrue(result.is_rate_card)

    def test_card_wins_on_the_long_route_too(self):
        """Disney -> Port Canaveral was the worst gap: card $185, formula $295."""
        result = qe.quote(
            vehicle_type="towncar",
            miles=Decimal("72"),
            minutes=69,
            pickup_location=self.locations["All WDW Disney Property Resorts"],
            dropoff_location=self.locations["Port Canaveral"],
        )
        self.assertEqual(result.price, Decimal("185"))

    def test_card_result_still_reports_the_unused_estimate(self):
        result = qe.quote(
            vehicle_type="towncar",
            miles=Decimal("20"),
            minutes=28,
            pickup_location=self.locations["Orlando International Airport"],
            dropoff_location=self.locations["All WDW Disney Property Resorts"],
        )
        self.assertEqual(
            result.breakdown["custom_estimate_not_used"], Decimal("135")
        )

    def test_vehicle_without_a_published_rate_falls_to_the_formula(self):
        """Only towncar/van are published for Disney -> Port Canaveral."""
        result = qe.quote(
            vehicle_type="suv",
            miles=Decimal("72"),
            minutes=69,
            pickup_location=self.locations["All WDW Disney Property Resorts"],
            dropoff_location=self.locations["Port Canaveral"],
        )
        self.assertEqual(result.source, qe.SOURCE_FORMULA)

    def test_off_card_in_area_route_is_local_not_out_of_area(self):
        """Winter Park -> MCO is 13 mi with no published route. It is in-area, so
        it gets the local floor rather than the out-of-area formula's $135
        dispatch minimum and empty return."""
        winter_park = Location.objects.create(name="Winter Park")
        result = qe.quote(
            vehicle_type="towncar",
            miles=Decimal("13"),
            minutes=20,
            pickup_location=winter_park,
            dropoff_location=self.locations["Orlando International Airport"],
        )
        self.assertEqual(result.source, qe.SOURCE_LOCAL_CUSTOM)
        self.assertEqual(result.price, Decimal("110"))
        self.assertNotIn("empty_return_share", result.breakdown)

    def test_off_card_out_of_area_route_uses_the_formula(self):
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("235"), minutes=215,
            pickup_miles_from_base=Decimal("230"),
        )
        self.assertEqual(result.source, qe.SOURCE_FORMULA)
        # $915 since the long-haul rate landed — see the gratuity test for why.
        self.assertEqual(result.price, Decimal("915"))

    def test_same_location_both_ends_is_not_a_card_match(self):
        mco = self.locations["Orlando International Airport"]
        self.assertIsNone(qe.lookup_card_rate("towncar", mco, mco))

    def test_quote_all_vehicles_covers_every_tier(self):
        results = qe.quote_all_vehicles(miles=Decimal("50"), minutes=55)
        self.assertEqual(len(results), 5)
        self.assertEqual(
            [r.vehicle_type for r in results], qe.VEHICLE_TIER_ORDER
        )

    def test_quote_rejects_unknown_vehicle(self):
        with self.assertRaises(KeyError):
            qe.quote(vehicle_type="hovercraft", miles=Decimal("20"))

    def test_off_card_quote_requires_miles(self):
        with self.assertRaises(ValueError):
            qe.quote(vehicle_type="towncar", miles=None)


class LocalCustomPricingTests(RateCardFixtureMixin, TestCase):
    """Founder calibration, 2026-07-29.

    Grand Floridian -> 2596 Carrickton Cir, Orlando (21.8 mi, 33 min) — a
    residential address a few miles from MCO. He priced it by analogy to
    MCO <-> Disney, not from mileage, because that is where the address sits.
    """

    #                  MCO<->Disney card   founder's quote for the custom address
    FOUNDER = {
        "towncar":     ("105.00", "120"),
        "mini_van":    ("120.00", "135"),
        "suv":         ("140.00", "160"),
    }

    def setUp(self):
        self.build_card()
        self.disney = self.locations["All WDW Disney Property Resorts"]
        self.mco = self.locations["Orlando International Airport"]

    def _quote(self, vehicle_type, trip_type="oneway"):
        return qe.quote(
            vehicle_type=vehicle_type,
            trip_type=trip_type,
            miles=Decimal("21.8"),
            minutes=33,
            pickup_location=self.disney,   # matched a zone by name
            dropoff_location=None,         # residential — no name match
            snapped_pickup=self.disney,
            snapped_dropoff=self.mco,      # snapped: Carrickton Cir is near MCO
        )

    def test_reproduces_every_founder_quote(self):
        for vehicle_type, (_card, expected) in self.FOUNDER.items():
            with self.subTest(vehicle=vehicle_type):
                result = self._quote(vehicle_type)
                self.assertEqual(result.source, qe.SOURCE_LOCAL_CUSTOM)
                self.assertEqual(result.price, Decimal(expected))

    def test_premium_is_applied_over_the_comparable_card_price(self):
        result = self._quote("towncar")
        self.assertEqual(result.breakdown["comparable_card_price"], Decimal("105.00"))
        self.assertIn("Orlando International Airport", result.breakdown["comparable_route"])
        self.assertEqual(result.breakdown["custom_premium_pct"], Decimal("13.5"))

    def test_local_pricing_has_no_empty_return_component(self):
        """Founder: 'we probably don't need to think about the empty return for
        local roads.' The mileage formula's out/back split must not appear."""
        result = self._quote("towncar")
        for key in ("empty_return_share", "revenue_leg_share", "per_driven_mile"):
            self.assertNotIn(key, result.breakdown)
        # Notes are written for a dispatcher on a call, so assert on the meaning
        # rather than logistics vocabulary.
        self.assertTrue(any("private address" in n for n in result.notes))

    def test_local_beats_the_old_mileage_formula(self):
        """The mileage formula quoted $135 here — that was the complaint."""
        formula_price, _bd, _n = qe.formula_oneway("towncar", Decimal("21.8"), 33)
        self.assertEqual(formula_price, Decimal("135"))
        self.assertEqual(self._quote("towncar").price, Decimal("120"))

    def test_round_trip_applies_the_same_premium_to_the_card_rt(self):
        # Founder chose "same +13.5% on the card's round trip". Values are the
        # card RT x 1.135, rounded to $5:
        #   towncar  195 -> 221.33 -> 220
        #   mini_van 230 -> 261.05 -> 260
        #   suv      275 -> 312.13 -> 310
        for vehicle_type, expected in (
            ("towncar", "220"), ("mini_van", "260"), ("suv", "310"),
        ):
            with self.subTest(vehicle=vehicle_type):
                self.assertEqual(
                    self._quote(vehicle_type, "roundtrip").price, Decimal(expected)
                )

    def test_intra_zone_trip_uses_the_local_floor_not_the_dispatch_minimum(self):
        """MCO -> a residence 4 mi from MCO. Both ends resolve to the SAME zone,
        and a zone has no route to itself, so this used to fall through to the
        out-of-area formula: $135 dispatch minimum, an empty-return split that
        does not apply locally, and no gratuity line."""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("7.5"), minutes=15,
            pickup_location=self.mco,
            dropoff_location=None,
            snapped_pickup=self.mco,
            snapped_dropoff=self.mco,   # Carrickton Cir snaps back to MCO
        )
        self.assertEqual(result.source, qe.SOURCE_LOCAL_CUSTOM)
        self.assertEqual(result.price, Decimal("110"))
        self.assertNotIn("empty_return_share", result.breakdown)
        self.assertTrue(result.gratuity_suggested)

    def test_in_area_trip_is_priced_on_one_direction_of_driving(self):
        """No empty return locally, so a 20 mi in-area run prices off 20 miles of
        driving, not 40."""
        local = qe.quote(
            vehicle_type="towncar", miles=Decimal("20"), minutes=25,
            pickup_location=self.mco, snapped_pickup=self.mco,
            snapped_dropoff=self.mco,
        )
        out_of_area, _bd, _n = qe.formula_oneway("towncar", Decimal("20"), 25)
        self.assertEqual(local.source, qe.SOURCE_LOCAL_CUSTOM)
        self.assertLess(local.price, out_of_area)
        self.assertEqual(local.breakdown["per_driven_mile"], Decimal("1.68"))

    def test_a_43_mile_run_is_not_chainable_local_work(self):
        """Regression, 2026-08-25: 164 Monterey Cypress Blvd, Winter Haven (43
        mi / 58 min from a Disney-area pickup) was quoted as chainable local
        work — $130 towncar — because it landed inside the old 60 mi radius
        shared with direction classification. It isn't chainable: round trip it
        ties up a car for the better part of two hours, same as Tampa. It must
        fall through to the out-of-area formula, empty return and all."""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("43"), minutes=58,
            pickup_location=self.disney, snapped_pickup=self.disney,
            snapped_dropoff=None,
        )
        self.assertEqual(result.source, qe.SOURCE_FORMULA)
        self.assertIn("empty_return_share", result.breakdown)
        self.assertEqual(result.price, Decimal("205"))

    def test_long_trip_from_a_known_zone_is_still_out_of_area(self):
        """Disney -> Tampa starts at a real zone but is far past the service
        area, so it must keep the empty return."""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("85"), minutes=78,
            pickup_location=self.disney, snapped_pickup=self.disney,
            snapped_dropoff=None,
        )
        self.assertEqual(result.source, qe.SOURCE_FORMULA)
        self.assertEqual(result.price, Decimal("340"))
        self.assertFalse(result.gratuity_suggested)
        self.assertIn("empty_return_share", result.breakdown)

    def test_local_floor_holds_on_a_very_short_trip(self):
        """Founder: 'it can be 6 miles, but I will have to drive from my base 10
        miles, then 6 miles, then back to my base. So let's say $110.'"""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("6"), minutes=12,
            pickup_location=self.locations["Sea World"],
            snapped_pickup=self.locations["Sea World"],
            snapped_dropoff=self.disney,   # SeaWorld<->Disney card is $75
        )
        self.assertEqual(result.price, Decimal("110"))
        self.assertTrue(result.breakdown["minimum_applied"])
        self.assertTrue(any("minimum charge" in n for n in result.notes))

    def test_local_fares_carry_a_recommended_gratuity(self):
        """Quoted pre-gratuity so there is room for 20% on top."""
        result = self._quote("towncar")
        self.assertTrue(result.gratuity_suggested)
        self.assertEqual(result.breakdown["gratuity_pct"], Decimal("20"))
        self.assertEqual(result.breakdown["gratuity_amount"], Decimal("24.00"))
        self.assertEqual(result.breakdown["total_with_gratuity"], Decimal("144.00"))

    def test_out_of_town_gratuity_is_billed_on_top_of_the_fare(self):
        """Founder: "we would let the guest know it will be nine hundred and
        twenty dollars plus twenty percent gratuity." The engine price is the
        FARE; the 20% is added, not folded in."""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("235"), minutes=215,
            pickup_miles_from_base=Decimal("230"),
        )
        # 235 mi. This was $840 until the long-haul rate landed on 2026-07-29.
        # It HAD to move: the founder's floor is $850 at 218 mi, so a price that
        # rises with distance cannot be below $850 at 235 mi. $915 is what the
        # rate fitted to his Port Everglades numbers produces.
        self.assertEqual(result.price, Decimal("915"))
        self.assertTrue(result.gratuity_mandatory)
        self.assertFalse(result.gratuity_suggested)
        self.assertEqual(result.breakdown["gratuity_amount"], Decimal("183.00"))
        self.assertEqual(result.breakdown["total_with_gratuity"], Decimal("1098.00"))
        self.assertTrue(any("plus 20% gratuity" in n for n in result.notes))

    def test_gratuity_is_flagged_internally_as_margin_not_driver_pay(self):
        """Founder: the gratuity is an upsell — drivers get a flat or hourly rate
        on out-of-town work. Dispatchers must not discuss the split with guests."""
        result = qe.quote(vehicle_type="towncar", miles=Decimal("235"), minutes=215)
        joined = " ".join(result.notes)
        self.assertIn("margin, not a pass-through", joined)
        self.assertIn("Never discuss", joined)

    def test_fare_plus_gratuity_always_sums_to_the_total(self):
        for miles, minutes in ((Decimal("85"), 78), (Decimal("235"), 215),
                               (Decimal("140"), 130)):
            with self.subTest(miles=miles):
                result = qe.quote(
                    vehicle_type="towncar", miles=miles, minutes=minutes,
                )
                self.assertEqual(
                    result.price + result.breakdown["gratuity_amount"],
                    result.breakdown["total_with_gratuity"],
                )

    def test_tampa_fare_and_gratuity(self):
        result = qe.quote(vehicle_type="towncar", miles=Decimal("85"), minutes=78)
        self.assertEqual(result.price, Decimal("340"))
        self.assertEqual(result.breakdown["gratuity_amount"], Decimal("68.00"))
        self.assertEqual(result.breakdown["total_with_gratuity"], Decimal("408.00"))

    def test_local_gratuity_is_suggested_not_billed(self):
        """The two regimes must not be confused."""
        result = self._quote("towncar")
        self.assertTrue(result.gratuity_suggested)
        self.assertFalse(result.gratuity_mandatory)

    def test_exact_zone_match_still_wins_over_local_custom(self):
        """Both ends naming real zones is a CARD trip, not a premium one."""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("20"), minutes=28,
            pickup_location=self.mco, dropoff_location=self.disney,
            snapped_pickup=self.mco, snapped_dropoff=self.disney,
        )
        self.assertTrue(result.is_rate_card)
        self.assertEqual(result.price, Decimal("105.00"))

    def test_unsnappable_address_falls_back_to_the_formula(self):
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("235"), minutes=215,
            snapped_pickup=self.disney, snapped_dropoff=None,
        )
        self.assertEqual(result.source, qe.SOURCE_FORMULA)


class SnapToZoneTests(RateCardFixtureMixin, TestCase):
    def setUp(self):
        self.build_card()

    def test_snaps_to_the_nearest_zone(self):
        distances = {
            "Orlando International Airport, Orlando, FL": Decimal("4.1"),
            "Walt Disney World Resort, Lake Buena Vista, FL": Decimal("19.0"),
        }
        location, miles = qe.snap_to_zone(
            "2596 Carrickton Cir, Orlando, FL",
            list(Location.objects.all()),
            lambda zone: distances.get(zone),
        )
        self.assertEqual(location.name, "Orlando International Airport")
        self.assertEqual(miles, Decimal("4.1"))

    def test_refuses_to_snap_a_far_address(self):
        location, miles = qe.snap_to_zone(
            "505 Water St, Tampa, FL",
            list(Location.objects.all()),
            lambda zone: Decimal("78"),
        )
        self.assertIsNone(location)
        self.assertIsNone(miles)

    def test_handles_unmeasurable_zones(self):
        location, _miles = qe.snap_to_zone(
            "somewhere", list(Location.objects.all()), lambda zone: None
        )
        self.assertIsNone(location)

    def test_blank_address_snaps_to_nothing(self):
        self.assertEqual(
            qe.snap_to_zone("", list(Location.objects.all()), lambda z: Decimal("1")),
            (None, None),
        )


class FullPublishedCardTests(TestCase):
    """Every price in the shipped rate card must come back verbatim.

    Loads rates_data.json rather than a hand-copied subset, so if the card is
    re-exported with new routes or prices this test covers them automatically.
    Skips if the fixture is not present.
    """

    FIXTURE = Path(settings.BASE_DIR) / "rates_data.json"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.FIXTURE.exists():
            raise SkipTest(f"{cls.FIXTURE.name} not present")

    def setUp(self):
        raw = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        vehicles, locations, routes = {}, {}, {}
        for obj in raw:
            if obj["model"] == "rates.vehicle":
                f = obj["fields"]
                vehicles[obj["pk"]] = Vehicle.objects.create(
                    vehicle_type=f["vehicle_type"],
                    capacity=f["capacity"],
                    luggage_capacity=f["luggage_capacity"],
                )
            elif obj["model"] == "rates.location":
                f = obj["fields"]
                locations[obj["pk"]] = Location.objects.create(
                    name=f["name"], aliases=f.get("aliases") or ""
                )
        for obj in raw:
            if obj["model"] == "rates.route":
                f = obj["fields"]
                routes[obj["pk"]] = Route.objects.create(
                    origin=locations[f["origin"]],
                    destination=locations[f["destination"]],
                )
        self.published = []
        for obj in raw:
            if obj["model"] == "rates.rate":
                f = obj["fields"]
                vehicle, route = vehicles[f["vehicle"]], routes[f["route"]]
                Rate.objects.create(
                    vehicle=vehicle,
                    route=route,
                    oneway_price=Decimal(f["oneway_price"]),
                    round_trip_price=Decimal(f["round_trip_price"]),
                )
                self.published.append(
                    (
                        vehicle.vehicle_type,
                        route,
                        Decimal(f["oneway_price"]),
                        Decimal(f["round_trip_price"]),
                    )
                )

    def test_fixture_actually_has_the_card(self):
        self.assertGreaterEqual(len(self.published), 50)

    def test_every_published_price_wins_over_the_formula(self):
        """Quoting a route in its own stored direction must return that row's
        price, never the formula. Reverse-direction lookup is covered separately,
        because the shipped card contains one self-contradicting pair."""
        for vehicle_type, route, oneway, roundtrip in self.published:
            if vehicle_type not in qe.VEHICLE_RATES:
                continue
            for trip_type, expected in (("oneway", oneway), ("roundtrip", roundtrip)):
                with self.subTest(
                    vehicle=vehicle_type, route=str(route), trip=trip_type
                ):
                    result = qe.quote(
                        vehicle_type=vehicle_type,
                        trip_type=trip_type,
                        # A PLAUSIBLE card-route distance. This used to pass an
                        # absurd 500 mi to prove the card was winning, but
                        # MAX_CARD_ROUTE_MI now (correctly) refuses a card match
                        # on a trip that long — that guard is what stops every
                        # Florida seaport being quoted at the Port Canaveral
                        # price. is_rate_card below is the real assertion.
                        miles=Decimal("25"),
                        minutes=35,
                        pickup_location=route.origin,
                        dropoff_location=route.destination,
                    )
                    self.assertTrue(result.is_rate_card)
                    self.assertEqual(result.price, expected)

    def test_reverse_direction_also_never_falls_to_the_formula(self):
        for vehicle_type, route, _ow, _rt in self.published:
            if vehicle_type not in qe.VEHICLE_RATES:
                continue
            with self.subTest(vehicle=vehicle_type, route=str(route)):
                result = qe.quote(
                    vehicle_type=vehicle_type,
                    miles=Decimal("25"),
                    minutes=35,
                    pickup_location=route.destination,
                    dropoff_location=route.origin,
                )
                self.assertTrue(result.is_rate_card)

    def test_self_contradicting_card_pair_is_flagged(self):
        """The shipped card prices mini_van Disney -> Universal at $190 but
        Universal -> Disney at $100, on a ~12 mi hop where the SUV is $105 and
        the Van is $115. That is a data error. Until it is corrected the engine
        must quote the direction of travel AND say so, not silently pick."""
        disney = Location.objects.get(name="All WDW Disney Property Resorts")
        universal = Location.objects.get(name="Universal Studios Area Hotels")

        forward = qe.quote(
            vehicle_type="mini_van", miles=Decimal("12"), minutes=24,
            pickup_location=disney, dropoff_location=universal,
        )
        backward = qe.quote(
            vehicle_type="mini_van", miles=Decimal("12"), minutes=24,
            pickup_location=universal, dropoff_location=disney,
        )
        self.assertEqual(forward.price, Decimal("190.00"))
        self.assertEqual(backward.price, Decimal("100.00"))
        for result, other in ((forward, "100.00"), (backward, "190.00")):
            self.assertEqual(
                result.breakdown["conflicting_reverse_card_price"], Decimal(other)
            )
            self.assertTrue(
                any("disagrees with itself" in n for n in result.notes),
                "dispatcher was not warned about the contradiction",
            )

    def test_agreeing_bidirectional_routes_are_not_flagged(self):
        """towncar Disney <-> Universal is $85 both ways — no warning."""
        disney = Location.objects.get(name="All WDW Disney Property Resorts")
        universal = Location.objects.get(name="Universal Studios Area Hotels")
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("12"), minutes=24,
            pickup_location=disney, dropoff_location=universal,
        )
        self.assertEqual(result.price, Decimal("85.00"))
        self.assertNotIn("conflicting_reverse_card_price", result.breakdown)


# ════════════════════════════════════════════════════════════════════════════
# The endpoint (Google Distance Matrix mocked — no network in tests)
# ════════════════════════════════════════════════════════════════════════════


class QuoteCalculatorEndpointTests(RateCardFixtureMixin, TestCase):
    def setUp(self):
        self.build_card()
        self.url = reverse("quote_calculator_api")
        self.admin = User.objects.create_superuser(
            username="founder", email="founder@example.com", password="pw12345!"
        )
        self.staff = User.objects.create_user(
            username="dispatcher", password="pw12345!", is_staff=True
        )

    def _post(self, **payload):
        payload.setdefault("pickup", "Orlando International Airport, FL")
        payload.setdefault("dropoff", "Disney's Grand Floridian Resort")
        payload.setdefault("vehicle", "towncar")
        payload.setdefault("trip_type", "oneway")
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json"
        )

    def _drive(self, distance_text, duration_seconds):
        return {
            "distance_text": distance_text,
            "duration_text": "n/a",
            "duration_seconds": duration_seconds,
        }

    # ── access control: still superuser-only, on purpose ──

    def test_anonymous_is_redirected(self):
        self.assertEqual(self._post().status_code, 302)

    def test_ordinary_staff_dispatcher_can_use_it(self):
        """Opened to all dispatchers on 2026-07-29. The page's standing
        "Demo - still in progress" banner is what makes that safe while rates
        are still being calibrated."""
        self.client.force_login(self.staff)
        with patch(
            "drivers.utils.get_drive_time", return_value=self._drive("20.1 mi", 1680)
        ):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("error", response.json())

    def test_page_loads_for_an_ordinary_dispatcher(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("quote_calculator"))
        self.assertEqual(response.status_code, 200)

    def test_non_staff_user_is_still_redirected(self):
        """Guests and drivers must not reach it."""
        outsider = User.objects.create_user(username="guest1", password="pw12345!")
        self.client.force_login(outsider)
        self.assertEqual(
            self.client.get(reverse("quote_calculator")).status_code, 302
        )
        self.assertEqual(self._post().status_code, 403)

    # ── behaviour ──

    def test_rate_card_route_returns_the_card_price(self):
        self.client.force_login(self.admin)
        with patch(
            "drivers.utils.get_drive_time", return_value=self._drive("20.1 mi", 1680)
        ):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "rate_card")
        self.assertEqual(body["price"], "105.00")
        self.assertEqual(body["matched_pickup"], "Orlando International Airport")
        self.assertEqual(body["matched_dropoff"], "All WDW Disney Property Resorts")

    def test_far_off_card_route_uses_the_out_of_area_formula(self):
        self.client.force_login(self.admin)
        with patch(
            "drivers.utils.get_drive_time", return_value=self._drive("235 mi", 12900)
        ):
            response = self._post(
                pickup="742 Evergreen Terrace, Springfield FL",
                dropoff="9999 Nowhere Rd, Elsewhere FL",
            )
        body = response.json()
        self.assertEqual(body["source"], "formula")
        self.assertIsNone(body["matched_pickup"])
        self.assertFalse(body["gratuity_suggested"])

    def test_feet_distance_does_not_blow_up_the_price(self):
        """'285 ft' used to parse as 285 MILES, quoting ~$1,010 for a trip across
        a parking lot. Assert the magnitude, not an exact figure — with the mock
        returning one distance for every lookup, the zone snapping can land on a
        card comparable, and either a floor or a card price is a correct answer.
        """
        self.client.force_login(self.admin)
        with patch(
            "drivers.utils.get_drive_time", return_value=self._drive("285 ft", 60)
        ):
            response = self._post(
                pickup="Rosen Plaza Hotel, 9700 International Dr",
                dropoff="742 Evergreen Terrace, Springfield FL",
            )
        price = Decimal(response.json()["price"])
        self.assertLess(price, Decimal("300"))   # 285 mi would be ~$1,010
        self.assertGreaterEqual(price, Decimal("100"))

    def test_internal_breakdown_is_present_for_custom_trips(self):
        self.client.force_login(self.admin)
        with patch(
            "drivers.utils.get_drive_time", return_value=self._drive("235 mi", 12900)
        ):
            response = self._post(
                pickup="742 Evergreen Terrace, Springfield FL",
                dropoff="9999 Nowhere Rd, Elsewhere FL",
            )
        body = response.json()
        self.assertIn("empty_return_share", body["internal"])
        self.assertIn("revenue_leg_share", body["internal"])
        self.assertTrue(body["notes"])

    def test_unknown_vehicle_is_rejected_not_priced_as_towncar(self):
        self.client.force_login(self.admin)
        response = self._post(vehicle="stretch_limo")
        self.assertIn("error", response.json())

    def test_missing_addresses_are_rejected(self):
        self.client.force_login(self.admin)
        self.assertIn("error", self._post(pickup="", dropoff="").json())

    def test_unroutable_addresses_surface_an_error(self):
        self.client.force_login(self.admin)
        with patch("drivers.utils.get_drive_time", return_value=None):
            self.assertIn("error", self._post().json())

    def test_all_vehicles_are_returned(self):
        self.client.force_login(self.admin)
        with patch(
            "drivers.utils.get_drive_time", return_value=self._drive("20.1 mi", 1680)
        ):
            body = self._post().json()
        self.assertEqual(len(body["all_vehicles"]), 5)

    def test_page_loads_for_superuser(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("quote_calculator"))
        self.assertEqual(response.status_code, 200)


class AirportPickupFeeTests(RateCardFixtureMixin, TestCase):
    """Founder, 2026-07-29: "for airport pickups always add an additional fee in
    the backend since we have to go thru commercial lane/tunnel. If it's point to
    point not airport, you can take that fee. $40 for long distances, $20 for
    short." Built into the fare, never itemised to the guest.
    """

    def setUp(self):
        self.build_card()

    def test_detects_real_airport_addresses(self):
        for address in (
            "Miami International Airport (MIA), Miami, FL, USA",
            "Orlando International Airport (MCO), Jeff Fuqua Blvd",
            "Fort Lauderdale-Hollywood Intl Airport, FL",
            "Tampa International Airport, Tampa, FL",
        ):
            with self.subTest(address=address):
                self.assertTrue(qe.is_airport_pickup(address))

    def test_does_not_fire_on_non_airports(self):
        for address in (
            "Port Everglades Cruise Terminal, Fort Lauderdale, FL",
            "2596 Carrickton Cir, Orlando, FL",
            "Walt Disney World Grand Floridian",
            "",
            None,
        ):
            with self.subTest(address=address):
                self.assertFalse(qe.is_airport_pickup(address))

    def test_airport_road_is_not_an_airport(self):
        """A street named Airport Rd is an ordinary address."""
        for address in ("1234 Airport Rd, Sanford, FL",
                        "500 Airport Boulevard, Orlando FL"):
            with self.subTest(address=address):
                self.assertFalse(qe.is_airport_pickup(address))

    def test_long_trip_gets_forty(self):
        base = qe.quote(vehicle_type="towncar", miles=Decimal("235"), minutes=215)
        with_fee = qe.quote(
            vehicle_type="towncar", miles=Decimal("235"), minutes=215,
            airport_pickup=True,
        )
        self.assertEqual(with_fee.price - base.price, Decimal("40"))
        self.assertEqual(with_fee.breakdown["airport_pickup_fee"], Decimal("40.00"))

    def test_short_trip_gets_twenty(self):
        mco = self.locations["Orlando International Airport"]
        base = qe.quote(
            vehicle_type="towncar", miles=Decimal("7.5"), minutes=15,
            pickup_location=mco, snapped_pickup=mco, snapped_dropoff=mco,
        )
        with_fee = qe.quote(
            vehicle_type="towncar", miles=Decimal("7.5"), minutes=15,
            pickup_location=mco, snapped_pickup=mco, snapped_dropoff=mco,
            airport_pickup=True,
        )
        self.assertEqual(base.price, Decimal("110"))
        self.assertEqual(with_fee.price, Decimal("130"))
        self.assertEqual(with_fee.breakdown["airport_pickup_fee"], Decimal("20.00"))

    def test_published_card_price_is_never_altered(self):
        """MCO -> Disney is an airport pickup, but $105 is what the website
        charges and already absorbs the airport's cost. Adding to it would quote
        above the website and break rate-card precedence."""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("20"), minutes=28,
            pickup_location=self.locations["Orlando International Airport"],
            dropoff_location=self.locations["All WDW Disney Property Resorts"],
            airport_pickup=True,
        )
        self.assertTrue(result.is_rate_card)
        self.assertEqual(result.price, Decimal("105.00"))
        self.assertNotIn("airport_pickup_fee", result.breakdown)

    def test_fee_is_charged_once_on_a_round_trip(self):
        """A round trip collects at the airport once and drops at departures."""
        base = qe.quote(
            vehicle_type="towncar", miles=Decimal("235"), minutes=215,
            trip_type="roundtrip",
        )
        with_fee = qe.quote(
            vehicle_type="towncar", miles=Decimal("235"), minutes=215,
            trip_type="roundtrip", airport_pickup=True,
        )
        self.assertEqual(with_fee.price - base.price, Decimal("40"))

    def test_port_everglades_anchor_has_no_airport_fee(self):
        """Disney -> Port Everglades is a cruise port, not an airport — the
        founder's $850/$920/$1400 must stand unchanged."""
        for vehicle_type, expected in (
            ("towncar", "850"), ("suv", "920"), ("Van(14 Pax)", "1400"),
        ):
            with self.subTest(vehicle=vehicle_type):
                result = qe.quote(
                    vehicle_type=vehicle_type, miles=Decimal("218"), minutes=194,
                    airport_pickup=qe.is_airport_pickup(
                        "Port Everglades Cruise Terminal, Fort Lauderdale, FL"
                    ),
                )
                self.assertEqual(result.price, Decimal(expected))

    def test_gratuity_is_calculated_on_the_fare_including_the_fee(self):
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("235"), minutes=215,
            airport_pickup=True,
        )
        self.assertEqual(result.price, Decimal("955"))
        self.assertEqual(result.breakdown["gratuity_amount"], Decimal("191.00"))
        self.assertEqual(result.breakdown["total_with_gratuity"], Decimal("1146.00"))


class SeaportOvermatchRegressionTests(RateCardFixtureMixin, TestCase):
    """Regression: every Florida seaport collapsed onto Port Canaveral.

    Port Canaveral's DB aliases include the generic "Cruise Terminal",
    "Cruise Port", "Carnival" and "Royal Caribbean". So "Port Everglades Cruise
    Terminal, Fort Lauderdale" matched the Port Canaveral zone, and a 218 mi run
    to Fort Lauderdale was quoted at the published $185 instead of $850 —
    labelled "this is our published rate", telling the dispatcher not to
    override it. A 78% underquote on the founder's calibrated route.

    The defence is MAX_CARD_ROUTE_MI, not the alias list: the card describes
    Orlando-area work whose longest route is ~72 mi, so any card match on a much
    longer trip means the address matcher over-matched.
    """

    def setUp(self):
        self.build_card()
        self.disney = self.locations["All WDW Disney Property Resorts"]
        self.canaveral = self.locations["Port Canaveral"]
        # Mirror the generic aliases that live in the production database.
        self.canaveral.aliases = (
            "Cape Canaveral, Cocoa Beach, Carnival, Royal Caribbean, "
            "Disney Cruise, Norwegian Cruise, Celebrity Cruises, "
            "Cruise Terminal, Cruise Port"
        )
        self.canaveral.save()

    FAR_PORTS = [
        "Port Everglades Cruise Terminal, Fort Lauderdale, FL",
        "PortMiami Cruise Terminal F, 1015 N America Way, Miami FL",
        "Royal Caribbean Terminal A, 1861 Eller Dr, Fort Lauderdale FL",
        "Carnival Cruise Terminal 2, 651 Channelside Dr, Tampa FL",
        "Port of Palm Beach Cruise Port, Riviera Beach FL",
    ]

    def test_far_seaports_are_not_quoted_at_the_canaveral_card_price(self):
        locations = list(Location.objects.all())
        for address in self.FAR_PORTS:
            with self.subTest(address=address):
                dropoff, _kw = qe.match_location(address, locations)
                result = qe.quote(
                    vehicle_type="towncar", miles=Decimal("218"), minutes=194,
                    pickup_location=self.disney, dropoff_location=dropoff,
                    snapped_pickup=self.disney, snapped_dropoff=dropoff,
                )
                self.assertEqual(result.source, qe.SOURCE_FORMULA)
                self.assertEqual(result.price, Decimal("850"))

    def test_the_dispatcher_is_told_why_the_card_was_rejected(self):
        """Silently repricing would be almost as bad — the dispatcher needs to
        know the address looked like a published route but could not be one."""
        locations = list(Location.objects.all())
        dropoff, _kw = qe.match_location(self.FAR_PORTS[0], locations)
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("218"), minutes=194,
            pickup_location=self.disney, dropoff_location=dropoff,
            snapped_pickup=self.disney, snapped_dropoff=dropoff,
        )
        joined = " ".join(result.notes)
        self.assertIn("far too long", joined)
        self.assertIn("seaport", joined)

    def test_the_real_port_canaveral_still_gets_its_card_price(self):
        """The guard must not break the route it protects."""
        result = qe.quote(
            vehicle_type="towncar", miles=Decimal("72"), minutes=60,
            pickup_location=self.disney, dropoff_location=self.canaveral,
            snapped_pickup=self.disney, snapped_dropoff=self.canaveral,
        )
        self.assertTrue(result.is_rate_card)
        self.assertEqual(result.price, Decimal("185.00"))

    def test_every_published_route_stays_under_the_guard(self):
        """If any real card route were longer than MAX_CARD_ROUTE_MI the guard
        would silently disable it, so pin the assumption."""
        self.assertGreaterEqual(qe.MAX_CARD_ROUTE_MI, Decimal("80"))
        result = qe.quote(
            vehicle_type="towncar", miles=qe.MAX_CARD_ROUTE_MI, minutes=75,
            pickup_location=self.disney, dropoff_location=self.canaveral,
            snapped_pickup=self.disney, snapped_dropoff=self.canaveral,
        )
        self.assertTrue(result.is_rate_card)

    def test_generic_cruise_words_are_not_seeded_by_us(self):
        """Our own alias seeds must not add the ambiguous cruise terms back."""
        seeded = qe.DEFAULT_LOCATION_ALIASES["Port Canaveral"]
        lowered = [a.lower() for a in seeded]
        for banned in ("cruise terminal", "cruise port", "canaveral"):
            self.assertNotIn(banned, lowered)


class RoundTripAirportFeeTests(RateCardFixtureMixin, TestCase):
    """A round trip that ENDS at an airport still collects there on the way
    home, so it must carry the fee. Before the fix, the same two addresses
    priced $20 apart depending on which box they were typed into — and the page
    ships a swap button one click away."""

    def setUp(self):
        self.build_card()
        self.url = reverse("quote_calculator_api")
        self.staff = User.objects.create_user(
            username="disp2", password="pw12345!", is_staff=True
        )
        self.client.force_login(self.staff)

    def _roundtrip(self, pickup, dropoff):
        with patch("drivers.utils.get_drive_time", return_value={
            "distance_text": "9.0 mi", "duration_text": "18 mins",
            "duration_seconds": 1080,
        }):
            return self.client.post(
                self.url,
                data=json.dumps({
                    "pickup": pickup, "dropoff": dropoff,
                    "vehicle": "towncar", "trip_type": "roundtrip",
                }),
                content_type="application/json",
            ).json()

    def test_airport_at_either_end_of_a_round_trip_carries_the_fee(self):
        out = self._roundtrip(
            "Orlando International Airport (MCO), FL", "742 Evergreen Terrace FL"
        )
        back = self._roundtrip(
            "742 Evergreen Terrace FL", "Orlando International Airport (MCO), FL"
        )
        self.assertEqual(out["price"], back["price"])
        for body in (out, back):
            self.assertIn("airport_pickup_fee", body["internal"])
