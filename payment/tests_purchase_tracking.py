"""Tests for the Meta Purchase tracking fixes (branch: fix/meta-purchase-tracking).

FIX 1 — payment/templates/stripe/success.html guards the purchase <script> behind
        {% if purchase_data %} and defaults `value` to 0, so a missing/None
        purchase_data can never emit `value: ,` — a JS SyntaxError that would kill
        the GA4 dataLayer push and the fbq Purchase together.

FIX 2 — a stable Reservation.purchase_event_id is minted ONCE at booking
        (reservations.utils.extra_charges) and read by the browser pixel + every
        server-side Conversions API fire, so Meta dedupes them to ONE Purchase.
        Legacy reservations (empty id) fall back to the prior transaction-id
        behavior (the Stripe payment-intent id, else the reservation uuid).

Run:  ./manage.py test payment.tests_purchase_tracking
"""
import re
import uuid as uuidlib
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg
from reservations.utils import extra_charges


class _ResFixture:
    """Minimal reservation graph, mirroring dispatching.tests_gratuity_afterhours."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="John", last_name="Doe", email="john@example.com",
            phone_number="5551234567",
        )

    def _res(self, **kw):
        defaults = dict(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("180.00"),
            total_price=Decimal("180.00"),
        )
        defaults.update(kw)
        return Reservation.objects.create(**defaults)

    def _leg(self, res, **kw):
        defaults = dict(
            reservation=res, pickup_date=date(2026, 6, 1), pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed",
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class Fix1ValueGuardTests(_ResFixture, TestCase):
    """The success page never emits a broken `value:` and omits the whole
    purchase script when there is no purchase_data."""

    def test_no_q_param_omits_purchase_script(self):
        resp = self.client.get(reverse("payment_success"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # The bug: `value: ,` — must never appear.
        self.assertNotIn("value: ,", body)
        # Guard: the whole purchase block is omitted when purchase_data is None.
        self.assertNotIn("fbq('track', 'Purchase'", body)
        self.assertNotIn("event: 'purchase'", body)

    def test_invalid_q_is_swallowed_and_omits_purchase_script(self):
        # Non-existent (but well-formed) uuid -> get_object_or_404 raises, the
        # broad except leaves purchase_data=None, page still renders 200.
        resp = self.client.get(reverse("payment_success"), {"q": str(uuidlib.uuid4())})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn("value: ,", body)
        self.assertNotIn("fbq('track', 'Purchase'", body)

    @patch("reservations.conversions.send_purchase_event")
    def test_valid_reservation_renders_numeric_value(self, _mock_capi):
        res = self._res(purchase_event_id="a" * 32)
        resp = self.client.get(reverse("payment_success"), {"q": str(res.uuid)})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn("value: ,", body)
        self.assertIn("value: 180", body)           # float(total_price)
        self.assertIn("fbq('track', 'Purchase'", body)
        self.assertIn("event: 'purchase'", body)

    @patch("reservations.conversions.send_purchase_event")
    def test_zero_total_renders_zero_not_blank(self, _mock_capi):
        # $0 / comped ride: value falls to 0.0 (a valid number), never blank.
        res = self._res(base_price=Decimal("0.00"), total_price=Decimal("0.00"),
                        purchase_event_id="c" * 32)
        resp = self.client.get(reverse("payment_success"), {"q": str(res.uuid)})
        body = resp.content.decode()
        self.assertNotIn("value: ,", body)
        self.assertIn("value: 0", body)


class Fix2StableEventIdTests(_ResFixture, TestCase):
    """purchase_event_id is minted once at booking and reused everywhere."""

    def test_extra_charges_mints_event_id_when_empty(self):
        res = self._res()
        self.assertEqual(res.purchase_event_id, "")     # default
        self._leg(res)
        extra_charges(res)
        res.refresh_from_db()
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", res.purchase_event_id),
                        f"expected uuid4 hex, got {res.purchase_event_id!r}")

    def test_extra_charges_does_not_overwrite_existing(self):
        existing = "f" * 32
        res = self._res(purchase_event_id=existing)
        self._leg(res)
        extra_charges(res)
        res.refresh_from_db()
        self.assertEqual(res.purchase_event_id, existing)

    @patch("reservations.conversions.send_purchase_event")
    def test_success_page_uses_stored_event_id(self, _mock_capi):
        stored = "b" * 32
        res = self._res(purchase_event_id=stored)   # no Stripe payment exists
        resp = self.client.get(reverse("payment_success"), {"q": str(res.uuid)})
        body = resp.content.decode()
        # Browser eventID is fed from purchase_data.event_id == stored id.
        self.assertIn(stored, body)

    @patch("reservations.conversions.send_purchase_event")
    def test_legacy_reservation_falls_back_to_transaction_id(self, _mock_capi):
        # Empty stored id + no payment -> event_id falls back to the uuid,
        # exactly the pre-fix behavior (no regression for legacy rows).
        res = self._res()
        self.assertEqual(res.purchase_event_id, "")
        resp = self.client.get(reverse("payment_success"), {"q": str(res.uuid)})
        body = resp.content.decode()
        self.assertIn(str(res.uuid), body)
