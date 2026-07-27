"""Refund correction tests — fixing a refund that was processed as a Full
Cancellation when it should have been a Price Adjustment or Partial Cancellation.

Covers the correct_refund endpoint: leg restoration (status/payment recovered
from history, driver left blank), reservation reactivation, refund reclassification,
the audit note, the "money is never touched" guarantee, and permissions.

Run with:
  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_refund_correction
"""
import json
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from payment.models import Payment
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, RefundRequest, Reservation

FUTURE = timezone.localdate() + timedelta(days=7)

# Signals fan out side-effect work onto background daemon threads which race the
# test's own SQLite writes ("database table is locked"). None of it matters here,
# so neutralise the spawns at every binding site these endpoints touch.
_NOOP = lambda *a, **k: None
_bg_targets = [
    "reservations.utils._run_in_background",
    "drivers.signals._run_in_background",
    "dispatching.views._run_in_background",
]
_bg_patchers = []


def setUpModule():
    for target in _bg_targets:
        try:
            p = mock.patch(target, _NOOP)
            p.start()
            _bg_patchers.append(p)
        except (AttributeError, ModuleNotFoundError):
            pass


def tearDownModule():
    for p in _bg_patchers:
        p.stop()
    _bg_patchers.clear()


class _RefundFixtureMixin:
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
        cls.driver_user = User.objects.create_user(
            username="rf_driver", first_name="Dan", is_staff=False
        )
        cls.driver = Driver.objects.create(profile=cls.driver_user, driver_type="inhouse")

        cls.dispatcher = User.objects.create_user("rf_dispatcher", password="x", is_staff=True)
        cls.manager = User.objects.create_superuser("rf_manager", password="x")

    def _post(self, payload):
        return self.client.post(
            reverse("correct_refund"),
            json.dumps(payload), content_type="application/json",
        )

    def _make_full_cancelled(self, num_legs=2, refund_type="full_cancellation",
                             status="completed", amount="200.00"):
        """Build a reservation whose legs + reservation were taken down by a
        completed Full Cancellation (mirrors what process_refund does), with the
        pre-cancel state preserved in Leg history."""
        res = Reservation.objects.create(
            trip_type="round-trip", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("200.00"),
            total_price=Decimal("200.00"), status="confirmed",
        )
        legs = []
        for _ in range(num_legs):
            leg = Leg.objects.create(
                reservation=res, pickup_date=FUTURE, pickup_time=time(9, 0),
                pickup_location="MCO", dropoff_location="Disney", route=self.route,
                status="confirmed", driver=self.driver, payment_status="paid",
            )
            legs.append(leg)
        payment = Payment.objects.create(
            reservation=res, amount=Decimal("200.00"), status="paid",
            stripe_payment_intent_id="pi_test", refunded_amount=Decimal("0.00"),
        )
        rr = RefundRequest.objects.create(
            reservation=res, refund_type=refund_type, status=status,
            amount=Decimal(amount), reason="customer cancelled",
            requested_by=self.dispatcher, processed_by=self.manager,
            processed_at=timezone.now(),
        )
        rr.legs.set(legs)
        # Perform the cancellation so history has a cancelled row on top of the
        # original confirmed/paid row.
        for leg in legs:
            leg.status = "cancelled"
            leg.payment_status = "canceled"
            leg.driver = None
            leg.save()
        res.status = "cancelled"
        res.refund_status = "completed"
        res.save()
        return res, legs, payment, rr

    def _make_requested(self, refund_type="price_adjustment", amount="40.00"):
        """Build a reservation with 2 active legs, a paid payment, and a
        RefundRequest still awaiting approval (status='requested')."""
        res = Reservation.objects.create(
            trip_type="round-trip", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("200.00"),
            total_price=Decimal("200.00"), status="confirmed",
        )
        legs = []
        for _ in range(2):
            legs.append(Leg.objects.create(
                reservation=res, pickup_date=FUTURE, pickup_time=time(9, 0),
                pickup_location="MCO", dropoff_location="Disney", route=self.route,
                status="confirmed", driver=self.driver, payment_status="paid",
            ))
        Payment.objects.create(
            reservation=res, amount=Decimal("200.00"), status="paid",
            stripe_payment_intent_id="pi_req", refunded_amount=Decimal("0.00"),
        )
        rr = RefundRequest.objects.create(
            reservation=res, refund_type=refund_type, status="requested",
            amount=Decimal(amount), reason="please refund",
            requested_by=self.dispatcher,
        )
        if refund_type == "partial_cancellation":
            rr.legs.set([legs[0]])
        elif refund_type == "full_cancellation":
            rr.legs.set(legs)
        return res, legs, rr


class CorrectRefundTests(_RefundFixtureMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.manager)

    def test_full_to_price_adjustment_restores_everything(self):
        res, legs, payment, rr = self._make_full_cancelled()

        r = self._post({"refund_request_id": rr.id, "new_refund_type": "price_adjustment"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["success"])

        for leg in legs:
            leg.refresh_from_db()
            self.assertEqual(leg.status, "confirmed")       # recovered from history
            self.assertEqual(leg.payment_status, "paid")     # recovered from history
            self.assertIsNone(leg.driver)                    # left blank for manual reassign

        res.refresh_from_db()
        self.assertEqual(res.status, "confirmed")

        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "price_adjustment")
        self.assertEqual(rr.legs.count(), 0)
        self.assertIn("CORRECTED", rr.notes)

    def test_money_is_never_touched(self):
        res, legs, payment, rr = self._make_full_cancelled()

        self._post({"refund_request_id": rr.id, "new_refund_type": "price_adjustment"})

        payment.refresh_from_db()
        self.assertEqual(payment.refunded_amount, Decimal("0.00"))
        self.assertEqual(payment.status, "paid")

    def test_full_to_partial_keeps_selected_leg_cancelled(self):
        res, legs, payment, rr = self._make_full_cancelled(num_legs=2)
        keep, restore = legs[0], legs[1]

        r = self._post({
            "refund_request_id": rr.id,
            "new_refund_type": "partial_cancellation",
            "keep_cancelled_leg_ids": [keep.id],
        })
        self.assertEqual(r.status_code, 200, r.content)

        keep.refresh_from_db()
        restore.refresh_from_db()
        self.assertEqual(keep.status, "cancelled")
        self.assertEqual(restore.status, "confirmed")

        res.refresh_from_db()
        self.assertEqual(res.status, "confirmed")  # still has an active leg

        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "partial_cancellation")
        self.assertEqual(set(rr.legs.values_list("id", flat=True)), {keep.id})

    def test_partial_without_a_kept_leg_is_rejected(self):
        res, legs, payment, rr = self._make_full_cancelled()
        r = self._post({
            "refund_request_id": rr.id,
            "new_refund_type": "partial_cancellation",
            "keep_cancelled_leg_ids": [],
        })
        self.assertEqual(r.status_code, 400)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "full_cancellation")  # unchanged

    def test_invalid_corrected_type_rejected(self):
        res, legs, payment, rr = self._make_full_cancelled()
        r = self._post({"refund_request_id": rr.id, "new_refund_type": "full_cancellation"})
        self.assertEqual(r.status_code, 400)

    def test_only_completed_refunds_correctable(self):
        res, legs, payment, rr = self._make_full_cancelled(status="requested")
        r = self._post({"refund_request_id": rr.id, "new_refund_type": "price_adjustment"})
        self.assertEqual(r.status_code, 400)

    def test_only_full_cancellations_correctable(self):
        res, legs, payment, rr = self._make_full_cancelled(refund_type="price_adjustment")
        r = self._post({"refund_request_id": rr.id, "new_refund_type": "price_adjustment"})
        self.assertEqual(r.status_code, 400)

    def test_requires_superuser(self):
        res, legs, payment, rr = self._make_full_cancelled()
        self.client.force_login(self.dispatcher)
        r = self._post({"refund_request_id": rr.id, "new_refund_type": "price_adjustment"})
        self.assertEqual(r.status_code, 403)
        rr.refresh_from_db()
        self.assertEqual(rr.refund_type, "full_cancellation")  # untouched


class BulkApproveRefundTests(_RefundFixtureMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.manager)

    def _bulk(self, ids):
        return self.client.post(
            reverse("bulk_approve_refunds"),
            json.dumps({"refund_request_ids": ids}), content_type="application/json",
        )

    @mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_1"))
    def test_bulk_approves_price_and_partial(self, mock_refund):
        _, _, rr1 = self._make_requested("price_adjustment", "40.00")
        _, legs2, rr2 = self._make_requested("partial_cancellation", "100.00")

        r = self._bulk([rr1.id, rr2.id])
        self.assertEqual(r.status_code, 200, r.content)
        j = r.json()
        self.assertEqual(j["approved_count"], 2)
        self.assertEqual(j["failed_count"], 0)

        rr1.refresh_from_db()
        rr2.refresh_from_db()
        self.assertEqual(rr1.status, "completed")
        self.assertEqual(rr2.status, "completed")

        # The partial cancellation cancelled its selected leg only.
        legs2[0].refresh_from_db()
        legs2[1].refresh_from_db()
        self.assertEqual(legs2[0].status, "cancelled")
        self.assertEqual(legs2[1].status, "confirmed")
        self.assertTrue(mock_refund.called)

    @mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_1"))
    def test_full_cancellation_is_excluded_from_bulk(self, mock_refund):
        _, legs, rr = self._make_requested("full_cancellation", "200.00")

        r = self._bulk([rr.id])
        self.assertEqual(r.status_code, 200, r.content)
        j = r.json()
        self.assertEqual(j["approved_count"], 0)
        self.assertEqual(j["failed_count"], 1)

        rr.refresh_from_db()
        self.assertEqual(rr.status, "requested")   # never processed
        legs[0].refresh_from_db()
        self.assertEqual(legs[0].status, "confirmed")  # never cancelled
        self.assertFalse(mock_refund.called)           # never charged

    @mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_1"))
    def test_already_completed_is_skipped(self, mock_refund):
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        rr.status = "completed"
        rr.save()

        r = self._bulk([rr.id])
        j = r.json()
        self.assertEqual(j["approved_count"], 0)
        self.assertEqual(j["failed_count"], 1)
        self.assertFalse(mock_refund.called)

    def test_empty_selection_rejected(self):
        r = self._bulk([])
        self.assertEqual(r.status_code, 400)

    def test_requires_superuser(self):
        _, _, rr = self._make_requested("price_adjustment", "40.00")
        self.client.force_login(self.dispatcher)
        r = self._bulk([rr.id])
        self.assertEqual(r.status_code, 403)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "requested")


class SingleProcessRefundRegressionTests(_RefundFixtureMixin, TestCase):
    """The refactor extracted _execute_refund_approval; confirm single approval
    through process_refund still behaves as before."""

    def setUp(self):
        self.client.force_login(self.manager)

    def _approve(self, rr, refund_type=None):
        payload = {"refund_request_id": rr.id, "action": "approve"}
        if refund_type:
            payload["refund_type"] = refund_type
        return self.client.post(
            reverse("process_refund"),
            json.dumps(payload), content_type="application/json",
        )

    @mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_1"))
    def test_price_adjustment_approve_refunds_without_cancelling(self, mock_refund):
        res, legs, rr = self._make_requested("price_adjustment", "40.00")
        r = self._approve(rr)
        self.assertEqual(r.status_code, 200, r.content)
        rr.refresh_from_db()
        self.assertEqual(rr.status, "completed")
        legs[0].refresh_from_db()
        self.assertEqual(legs[0].status, "confirmed")  # untouched
        self.assertTrue(mock_refund.called)

    @mock.patch("dispatching.views.stripe.Refund.create", return_value=mock.Mock(id="re_1"))
    def test_full_cancellation_approve_cancels_everything(self, mock_refund):
        res, legs, rr = self._make_requested("full_cancellation", "200.00")
        r = self._approve(rr)
        self.assertEqual(r.status_code, 200, r.content)
        res.refresh_from_db()
        self.assertEqual(res.status, "cancelled")
        for leg in legs:
            leg.refresh_from_db()
            self.assertEqual(leg.status, "cancelled")
            self.assertIsNone(leg.driver)
