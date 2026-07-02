"""
Tests for the quote engine and the quote→booking hand-off.

Covers the acceptance criteria:
  • named-route quote (Miami SUV = $950 + $190 = $1,140) with price_source=route_table
  • the named route fires ZERO Google Distance Matrix calls
  • unlisted route → per-mile formula, then the class floor
  • hourly = rate × hours, 3-hr minimum enforced (sub-minimum rejected)
  • peak multiplier applied on configured dates and not on others
  • the API endpoint + the quote-to-reservation hand-off (rate-less booking)
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pricing.models import (
    CityRoute,
    CityRoutePrice,
    FallbackFormula,
    InstantQuote,
    PeakDate,
    PricingConfig,
    RouteDistanceCache,
    VehicleClass,
)
from pricing.services import QuoteError, compute_quote, quote_all_classes
from reservations.models import Reservation


def future_date(days=30):
    return timezone.localdate() + timedelta(days=days)


class QuoteEngineBaseTest(TestCase):
    """The seed data migration (0002) provides classes, routes, hourly rates and
    fallback formulas in the test DB. We neutralize seeded peak dates so peak
    behavior is controlled per-test."""

    def setUp(self):
        PeakDate.objects.update(is_active=False)
        self.date = future_date(45)  # a controlled non-peak date


class NamedRouteTests(QuoteEngineBaseTest):
    def test_miami_suv_acceptance(self):
        r = compute_quote(
            service_type="city_to_city",
            vehicle_class_key="suv",
            service_date=self.date,
            origin="Orlando",
            destination="Miami",
        )
        self.assertEqual(r.price_source, "route_table")
        self.assertTrue(r.all_inclusive)
        self.assertEqual(r.base_price, Decimal("950"))
        self.assertIsNone(r.peak_adjustment)
        self.assertEqual(r.gratuity, Decimal("190"))
        self.assertEqual(r.total, Decimal("1140"))

    def test_miami_equals_fort_lauderdale(self):
        """Documented intentional parity — both seeded identical."""
        miami = compute_quote(service_type="city_to_city", vehicle_class_key="suv",
                              service_date=self.date, origin="Orlando", destination="Miami")
        fll = compute_quote(service_type="city_to_city", vehicle_class_key="suv",
                            service_date=self.date, origin="Orlando", destination="Fort Lauderdale")
        self.assertEqual(miami.total, fll.total)

    def test_named_route_fires_zero_distance_matrix(self):
        """The headline cost guarantee: a named-route quote never calls Google."""
        with patch("pricing.distance._call_distance_matrix") as mock_dm:
            r = compute_quote(
                service_type="city_to_city",
                vehicle_class_key="towncar",
                service_date=self.date,
                origin="Orlando",
                destination="Tampa",
            )
        self.assertEqual(r.price_source, "route_table")
        mock_dm.assert_not_called()

    def test_route_matches_by_alias(self):
        r = compute_quote(service_type="city_to_city", vehicle_class_key="towncar",
                          service_date=self.date, origin="Orlando", destination="MIA")
        self.assertEqual(r.price_source, "route_table")
        self.assertEqual(r.base_price, Decimal("675"))


class FormulaRouteTests(QuoteEngineBaseTest):
    def test_unlisted_route_uses_formula_from_cache(self):
        # Seed a cached distance so no live call is needed.
        RouteDistanceCache.objects.create(
            origin_key="orlando", destination_key="savannah ga",
            origin_text="Orlando", destination_text="Savannah, GA",
            miles=Decimal("280"), source="seed",
        )
        with patch("pricing.distance._call_distance_matrix") as mock_dm:
            r = compute_quote(
                service_type="city_to_city", vehicle_class_key="suv",
                service_date=self.date, origin="Orlando", destination="Savannah, GA",
            )
        mock_dm.assert_not_called()
        self.assertEqual(r.price_source, "formula")
        # SUV: base 170 + (280 loaded * 1.8 deadhead = 504) * 2.65 = 170 + 1335.6 = 1505.6 -> 1506
        self.assertEqual(r.base_price, Decimal("1506"))
        self.assertEqual(r.gratuity, Decimal("301"))  # round(1506*0.20=301.2)
        self.assertEqual(r.total, Decimal("1807"))
        # the customer still sees the true one-way distance, not the inflated miles
        self.assertEqual(r.loaded_miles, Decimal("280"))

    def test_formula_floor_applies(self):
        RouteDistanceCache.objects.create(
            origin_key="orlando", destination_key="kissimmee",
            origin_text="Orlando", destination_text="Kissimmee",
            miles=Decimal("10"), source="seed",
        )
        r = compute_quote(service_type="city_to_city", vehicle_class_key="towncar",
                          service_date=self.date, origin="Orlando", destination="Kissimmee")
        # Towncar: 145 + (10*1.8=18)*2.05 = 145 + 36.9 = 181.9 -> below floor 250 -> 250
        self.assertEqual(r.base_price, Decimal("250"))

    def test_deadhead_factor_inflates_mileage(self):
        RouteDistanceCache.objects.create(
            origin_key="orlando", destination_key="testtown",
            origin_text="Orlando", destination_text="Testtown",
            miles=Decimal("200"), source="seed",
        )
        f = FallbackFormula.objects.get(vehicle_class__key="towncar")
        f.deadhead_factor = Decimal("2.0")
        f.save()
        r = compute_quote(service_type="city_to_city", vehicle_class_key="towncar",
                          service_date=self.date, origin="Orlando", destination="Testtown")
        # towncar: 145 + (200*2.0=400)*2.05 = 145 + 820 = 965
        self.assertEqual(r.base_price, Decimal("965"))
        self.assertEqual(r.loaded_miles, Decimal("200"))  # true one-way distance shown

    def test_deadhead_does_not_affect_named_routes(self):
        # A named-route flat price must not move when the deadhead factor changes.
        f = FallbackFormula.objects.get(vehicle_class__key="suv")
        f.deadhead_factor = Decimal("3.0")
        f.save()
        r = compute_quote(service_type="city_to_city", vehicle_class_key="suv",
                          service_date=self.date, origin="Orlando", destination="Miami")
        self.assertEqual(r.price_source, "route_table")
        self.assertEqual(r.base_price, Decimal("950"))  # unchanged

    @override_settings(PRICING_ALLOW_LIVE_DISTANCE=True, GOOGLE_MAPS_API_KEY="test")
    def test_live_distance_called_once_then_cached(self):
        with patch("pricing.distance._call_distance_matrix", return_value=Decimal("100")) as mock_dm:
            compute_quote(service_type="city_to_city", vehicle_class_key="towncar",
                          service_date=self.date, origin="Orlando", destination="Vero Beach")
            compute_quote(service_type="city_to_city", vehicle_class_key="towncar",
                          service_date=self.date, origin="Orlando", destination="Vero Beach")
        self.assertEqual(mock_dm.call_count, 1)  # second quote hits the cache
        self.assertTrue(RouteDistanceCache.objects.filter(destination_key="vero beach").exists())

    @override_settings(PRICING_ALLOW_LIVE_DISTANCE=False)
    def test_uncached_unlisted_route_without_live_raises(self):
        with self.assertRaises(QuoteError) as ctx:
            compute_quote(service_type="city_to_city", vehicle_class_key="towncar",
                          service_date=self.date, origin="Orlando", destination="Nowhereville, ZZ")
        self.assertEqual(ctx.exception.code, "distance_unavailable")


class HourlyTests(QuoteEngineBaseTest):
    def test_hourly_basic(self):
        r = compute_quote(service_type="hourly", vehicle_class_key="towncar",
                          service_date=self.date, hours=Decimal("3"))
        self.assertEqual(r.price_source, "hourly")
        self.assertFalse(r.all_inclusive)
        self.assertEqual(r.base_price, Decimal("345"))  # 115*3
        self.assertEqual(r.gratuity, Decimal("69"))
        self.assertEqual(r.total, Decimal("414"))

    def test_hourly_below_minimum_rejected(self):
        with self.assertRaises(QuoteError) as ctx:
            compute_quote(service_type="hourly", vehicle_class_key="towncar",
                          service_date=self.date, hours=Decimal("2"))
        self.assertEqual(ctx.exception.code, "below_minimum")
        self.assertEqual(ctx.exception.field, "hours")

    def test_hourly_overtime_increment_enforced(self):
        with self.assertRaises(QuoteError) as ctx:
            compute_quote(service_type="hourly", vehicle_class_key="towncar",
                          service_date=self.date, hours=Decimal("3.25"))
        self.assertEqual(ctx.exception.code, "bad_increment")

    def test_hourly_half_hour_overtime_ok(self):
        r = compute_quote(service_type="hourly", vehicle_class_key="suv",
                          service_date=self.date, hours=Decimal("3.5"))
        self.assertEqual(r.base_price, Decimal("473"))  # round(135*3.5=472.5)=473 (half-up)

    def test_sprinter_peak_minimum_hours(self):
        # On a peak date the Sprinter minimum rises from 3 to 4.
        peak = future_date(60)
        PeakDate.objects.create(label="Test Peak", start_date=peak, end_date=peak,
                                multiplier=Decimal("1.25"), is_active=True)
        with self.assertRaises(QuoteError) as ctx:
            compute_quote(service_type="hourly", vehicle_class_key="sprinter",
                          service_date=peak, hours=Decimal("3"))
        self.assertEqual(ctx.exception.code, "below_minimum")
        # 4 hours is accepted on the peak date.
        r = compute_quote(service_type="hourly", vehicle_class_key="sprinter",
                          service_date=peak, hours=Decimal("4"))
        self.assertIsNotNone(r.peak_adjustment)


class PeakTests(QuoteEngineBaseTest):
    def test_peak_applied_only_on_peak_dates(self):
        peak = future_date(70)
        PeakDate.objects.create(label="Holiday", start_date=peak, end_date=peak,
                                multiplier=Decimal("1.25"), is_active=True)

        normal = compute_quote(service_type="city_to_city", vehicle_class_key="suv",
                               service_date=self.date, origin="Orlando", destination="Miami")
        self.assertIsNone(normal.peak_adjustment)
        self.assertEqual(normal.total, Decimal("1140"))

        on_peak = compute_quote(service_type="city_to_city", vehicle_class_key="suv",
                                service_date=peak, origin="Orlando", destination="Miami")
        # base 950, peaked 950*1.25=1187.5 -> 1188; adj = 1188-950 = 238
        self.assertEqual(on_peak.base_price, Decimal("950"))
        self.assertEqual(on_peak.peak_adjustment, Decimal("238"))
        self.assertEqual(on_peak.gratuity, Decimal("238"))  # round(1188*0.20=237.6)
        self.assertEqual(on_peak.total, Decimal("1426"))

    def test_per_event_multiplier_override(self):
        peak = future_date(80)
        PeakDate.objects.create(label="Big Event", start_date=peak, end_date=peak,
                                multiplier=Decimal("1.50"), is_active=True)
        r = compute_quote(service_type="hourly", vehicle_class_key="towncar",
                          service_date=peak, hours=Decimal("3"))
        # base 345, peaked 345*1.5=517.5 -> 518; adj 173
        self.assertEqual(r.peak_adjustment, Decimal("173"))


class ValidationTests(QuoteEngineBaseTest):
    def test_unknown_class(self):
        with self.assertRaises(QuoteError) as ctx:
            compute_quote(service_type="city_to_city", vehicle_class_key="limo",
                          service_date=self.date, origin="Orlando", destination="Miami")
        self.assertEqual(ctx.exception.code, "unknown_class")

    def test_missing_destination(self):
        with self.assertRaises(QuoteError) as ctx:
            compute_quote(service_type="city_to_city", vehicle_class_key="suv",
                          service_date=self.date, origin="Orlando", destination="")
        self.assertEqual(ctx.exception.code, "missing_destination")

    def test_past_date_rejected(self):
        with self.assertRaises(QuoteError) as ctx:
            compute_quote(service_type="hourly", vehicle_class_key="suv",
                          service_date=future_date(-2), hours=Decimal("3"))
        self.assertEqual(ctx.exception.code, "past_date")


class QuoteApiTests(QuoteEngineBaseTest):
    def test_api_named_route(self):
        resp = self.client.post(
            reverse("quote_api"),
            data=json.dumps({
                "service_type": "city_to_city", "vehicle_class": "suv",
                "date": self.date.isoformat(), "origin": "Orlando",
                "destination": "Miami", "route_id": CityRoute.objects.get(slug="miami-mia").id,
            }),
            content_type="application/json", SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 200)
        j = resp.json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["total"], 1140.0)
        self.assertEqual(j["price_source"], "route_table")
        self.assertTrue(j["token"])
        self.assertTrue(InstantQuote.objects.filter(token=j["token"]).exists())

    def test_api_validation_error(self):
        resp = self.client.post(
            reverse("quote_api"),
            data=json.dumps({"service_type": "hourly", "vehicle_class": "towncar",
                             "date": self.date.isoformat(), "hours": 1}),
            content_type="application/json", SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "below_minimum")


class HandoffTests(QuoteEngineBaseTest):
    def _make_quote(self):
        return InstantQuote.objects.create(
            service_type="city_to_city",
            vehicle_class=VehicleClass.objects.get(key="suv"),
            service_date=self.date, origin="Orlando", destination="Miami (MIA)",
            base_price=Decimal("950"), peak_adjustment=None,
            gratuity=Decimal("190"), total=Decimal("1140"),
            all_inclusive=True, price_source="route_table",
            expires_at=timezone.now() + timedelta(days=14),
        )

    def test_book_quote_creates_rateless_reservation(self):
        quote = self._make_quote()
        resp = self.client.post(
            reverse("book_quote", args=[quote.token]),
            data={
                "first_name": "Pat", "last_name": "Rivera",
                "email": "pat@example.com", "phone_number": "407-555-0100",
                "zipcode": "32801",
                "pickup_date": self.date.isoformat(), "pickup_time": "09:00",
                "pickup_location": "Grande Lakes Resort",
                "dropoff_location": "Miami, FL", "passenger_count": "3",
            },
            SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("checkout-session", resp["Location"])

        res = Reservation.objects.latest("id")
        self.assertIsNone(res.rate_id)  # rate-less booking
        self.assertEqual(res.service_type, "city_to_city")
        self.assertEqual(res.total_price, Decimal("1140"))
        self.assertEqual(res.base_price, Decimal("950"))
        self.assertEqual(res.gratuity_amount, Decimal("190"))
        self.assertEqual(res.legs.count(), 1)
        quote.refresh_from_db()
        self.assertEqual(quote.converted_reservation_id, res.id)

    def test_book_expired_quote_returns_410(self):
        quote = self._make_quote()
        quote.expires_at = timezone.now() - timedelta(minutes=1)
        quote.save(update_fields=["expires_at"])
        resp = self.client.get(reverse("book_quote", args=[quote.token]), SERVER_NAME="localhost")
        self.assertEqual(resp.status_code, 410)


class AllClassesQuoteTests(QuoteEngineBaseTest):
    """Blacklane-style preview: one trip in, every active class priced out,
    one distance lookup, and NO token minted until a class is selected."""

    def test_service_prices_every_active_class_one_lookup(self):
        RouteDistanceCache.objects.create(
            origin_key="orlando", destination_key="savannah ga",
            origin_text="Orlando", destination_text="Savannah, GA",
            miles=Decimal("280"), source="seed",
        )
        with patch("pricing.distance._call_distance_matrix") as mock_dm:
            quotes = quote_all_classes(
                service_type="city_to_city", service_date=self.date,
                origin="Orlando", destination="Savannah, GA",
            )
        mock_dm.assert_not_called()  # cache covered the single lookup
        active = VehicleClass.objects.filter(is_active=True).count()
        self.assertEqual(len(quotes), active)
        self.assertTrue(all(q.available for q in quotes))
        # every class priced off the same loaded miles, formula path
        self.assertTrue(all(q.result.price_source == "formula" for q in quotes))
        self.assertTrue(all(q.result.loaded_miles == Decimal("280") for q in quotes))

    def test_service_marks_class_without_route_price_unavailable(self):
        route = CityRoute.objects.get(slug="miami-mia")
        CityRoutePrice.objects.filter(
            city_route=route, vehicle_class__key="sprinter"
        ).delete()
        quotes = quote_all_classes(
            service_type="city_to_city", service_date=self.date,
            origin="Orlando", destination="Miami", route_id=route.id,
        )
        by_key = {q.vehicle_class.key: q for q in quotes}
        self.assertFalse(by_key["sprinter"].available)
        self.assertTrue(by_key["suv"].available)
        self.assertEqual(by_key["suv"].result.total, Decimal("1140"))

    def test_api_options_named_route(self):
        resp = self.client.post(
            reverse("quote_api"),
            data=json.dumps({
                "service_type": "city_to_city", "date": self.date.isoformat(),
                "origin": "Orlando", "destination": "Miami",
                "route_id": CityRoute.objects.get(slug="miami-mia").id,
            }),
            content_type="application/json", SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 200)
        j = resp.json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["mode"], "options")
        self.assertEqual(len(j["options"]), VehicleClass.objects.filter(is_active=True).count())
        suv = [o for o in j["options"] if o["vehicle_class"]["key"] == "suv"][0]
        self.assertTrue(suv["available"])
        self.assertEqual(suv["total"], 1140.0)
        self.assertEqual(suv["price_source"], "route_table")
        # the preview locks NOTHING — no InstantQuote until a class is selected
        self.assertEqual(InstantQuote.objects.count(), 0)

    def test_api_options_hourly(self):
        resp = self.client.post(
            reverse("quote_api"),
            data=json.dumps({
                "service_type": "hourly", "date": self.date.isoformat(), "hours": 3,
            }),
            content_type="application/json", SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 200)
        j = resp.json()
        towncar = [o for o in j["options"] if o["vehicle_class"]["key"] == "towncar"][0]
        self.assertEqual(towncar["total"], 414.0)  # 115*3=345 +20% gratuity

    @override_settings(PRICING_ALLOW_LIVE_DISTANCE=False)
    def test_api_options_distance_unavailable_is_trip_level_error(self):
        resp = self.client.post(
            reverse("quote_api"),
            data=json.dumps({
                "service_type": "city_to_city", "date": self.date.isoformat(),
                "origin": "Orlando", "destination": "Nowhereville, ZZ",
            }),
            content_type="application/json", SERVER_NAME="localhost",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "distance_unavailable")


class RouteLabelTests(QuoteEngineBaseTest):
    def test_reservation_route_label_null_safe(self):
        """The Stripe/email helper must not blow up on a rate-less reservation."""
        from reservations.models import Customer
        cust = Customer.objects.create(first_name="A", last_name="B",
                                       email="a@b.com", phone_number="407-555-0101")
        res = Reservation.objects.create(
            trip_type="one_way", service_type="hourly", customer=cust, rate=None,
            base_price=Decimal("345"), gratuity_amount=Decimal("69"),
            total_price=Decimal("414"), quoted_hours=Decimal("3"),
        )
        self.assertEqual(res.vehicle_label, "")
        self.assertIn("Hourly Charter", res.route_label)
