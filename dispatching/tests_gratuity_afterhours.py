"""Tests for the gratuity-charge attribution fix and the after-hours fee leak fix.

Run with:  ./manage.py test dispatching.tests_gratuity_afterhours

Covers:
  * Gratuity equal-split now divides only the UNATTRIBUTED remainder, so a tip
    pinned to one leg is never smeared across the others (Leg.save()).
  * The portal helper `_apply_gratuity_to_legs` (single leg / specific leg / whole).
  * After-hours helpers + the booking marker (no double-charge).
  * `flag_afterhours_fee` create / dedup / flap-back-close.
  * The `charge_afterhours_fee` endpoint (mocked Stripe).
"""
import json
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg, Flight
from reservations.utils import (
    AFTERHOURS_FEE_AMOUNT,
    afterhours_fee_owed,
    is_afterhours_time,
    extra_charges,
)
from ops.models import OperationalTask
from ops.tasks import flag_afterhours_fee
from drivers.models import Driver


def _make_driver(username):
    user = User.objects.create_user(username=username, first_name=username.title())
    return Driver.objects.create(profile=user, driver_type="inhouse")


class _BillingFixtureMixin:
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

    def _res(self, gratuity=Decimal("0.00"), base=Decimal("180.00")):
        return Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=base, total_price=base,
            gratuity_amount=gratuity,
        )

    def _leg(self, res, **kw):
        defaults = dict(
            reservation=res, pickup_date=date(2026, 6, 1), pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed",
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class GratuitySplitTests(_BillingFixtureMixin, TestCase):
    """Leg.save() auto-fill split now respects already-pinned per-leg gratuity."""

    def test_base_gratuity_splits_evenly_when_nothing_pinned(self):
        res = self._res(gratuity=Decimal("40.00"))
        leg1, leg2 = self._leg(res), self._leg(res)
        leg1.driver = _make_driver("g_d1")
        leg1.save()
        leg2.driver = _make_driver("g_d2")
        leg2.save()
        leg1.refresh_from_db()
        leg2.refresh_from_db()
        self.assertEqual(leg1.driver_gratuity, Decimal("20.00"))
        self.assertEqual(leg2.driver_gratuity, Decimal("20.00"))

    def test_pinned_tip_not_smeared_onto_other_leg(self):
        from dispatching.views import _apply_gratuity_to_legs

        res = self._res(gratuity=Decimal("0.00"))
        leg1, leg2 = self._leg(res), self._leg(res)
        # Pin $50 to leg1 (the portal gratuity flow) and bump the reservation total.
        res.gratuity_amount = Decimal("50.00")
        res.save(update_fields=["gratuity_amount"])
        _apply_gratuity_to_legs(res, Decimal("50.00"), str(leg1.id))
        leg1.refresh_from_db()
        self.assertEqual(leg1.driver_gratuity, Decimal("50.00"))

        # Assigning a driver to leg2 must NOT pull the pinned $50 onto it.
        leg2.driver = _make_driver("g_d3")
        leg2.save()
        leg2.refresh_from_db()
        self.assertEqual(leg2.driver_gratuity, Decimal("0.00"))


class ApplyGratuityToLegsTests(_BillingFixtureMixin, TestCase):
    """The portal helper attributes a charged tip to the right leg(s)."""

    def test_single_leg_gets_full_tip(self):
        from dispatching.views import _apply_gratuity_to_legs

        res = self._res()
        leg = self._leg(res)
        _apply_gratuity_to_legs(res, Decimal("30.00"), "whole")
        leg.refresh_from_db()
        self.assertEqual(leg.driver_gratuity, Decimal("30.00"))

    def test_specific_leg_target_gets_whole_tip(self):
        from dispatching.views import _apply_gratuity_to_legs

        res = self._res()
        leg1, leg2 = self._leg(res), self._leg(res)
        _apply_gratuity_to_legs(res, Decimal("40.00"), str(leg1.id))
        leg1.refresh_from_db()
        leg2.refresh_from_db()
        self.assertEqual(leg1.driver_gratuity, Decimal("40.00"))
        self.assertEqual(leg2.driver_gratuity or Decimal("0.00"), Decimal("0.00"))

    def test_whole_splits_evenly(self):
        from dispatching.views import _apply_gratuity_to_legs

        res = self._res()
        leg1, leg2 = self._leg(res), self._leg(res)
        _apply_gratuity_to_legs(res, Decimal("40.00"), "whole")
        leg1.refresh_from_db()
        leg2.refresh_from_db()
        self.assertEqual(leg1.driver_gratuity, Decimal("20.00"))
        self.assertEqual(leg2.driver_gratuity, Decimal("20.00"))

    def test_whole_odd_remainder_lands_on_last_leg(self):
        from dispatching.views import _apply_gratuity_to_legs

        res = self._res()
        leg1, leg2 = self._leg(res), self._leg(res)
        _apply_gratuity_to_legs(res, Decimal("0.05"), "whole")
        leg1.refresh_from_db()
        leg2.refresh_from_db()
        self.assertEqual(
            (leg1.driver_gratuity or Decimal("0")) + (leg2.driver_gratuity or Decimal("0")),
            Decimal("0.05"),
        )

    def test_split_records_a_note_on_each_leg(self):
        from dispatching.views import _apply_gratuity_to_legs

        res = self._res()
        leg1, leg2 = self._leg(res), self._leg(res)
        _apply_gratuity_to_legs(res, Decimal("46.00"), "whole")
        leg1.refresh_from_db()
        leg2.refresh_from_db()
        self.assertIn("$23.00 Gratuity Included", leg1.private_notes or "")
        self.assertIn("$23.00 Gratuity Included", leg2.private_notes or "")


class AfterhoursHelperTests(_BillingFixtureMixin, TestCase):
    def test_is_afterhours_time_window(self):
        self.assertTrue(is_afterhours_time(time(22, 0)))
        self.assertTrue(is_afterhours_time(time(23, 30)))
        self.assertTrue(is_afterhours_time(time(5, 59)))
        self.assertFalse(is_afterhours_time(time(6, 0)))
        self.assertFalse(is_afterhours_time(time(21, 59)))
        self.assertFalse(is_afterhours_time(None))

    def test_fee_owed(self):
        self.assertEqual(afterhours_fee_owed(time(22, 30)), AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(afterhours_fee_owed(time(12, 0)), Decimal("0.00"))

    def test_extra_charges_marks_leg_and_blocks_double_flag(self):
        res = self._res(base=Decimal("100.00"))
        leg = self._leg(res, pickup_time=time(23, 0))
        extra_charges(res)
        leg.refresh_from_db()
        res.refresh_from_db()
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(res.additional_charges, Decimal("20.00"))
        # An already-collected after-hours leg must not raise a flag.
        flag_afterhours_fee(leg, time(23, 0))
        self.assertEqual(
            OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.AFTERHOURS_FEE
            ).count(),
            0,
        )


class FlagAfterhoursTests(_BillingFixtureMixin, TestCase):
    def _open_count(self, leg):
        return OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ).count()

    def test_flag_creates_then_dedups(self):
        res = self._res()
        leg = self._leg(res, pickup_time=time(20, 0))  # booked early; not charged
        t1 = flag_afterhours_fee(leg, time(22, 30))
        self.assertIsNotNone(t1)
        self.assertEqual(t1.task_type, OperationalTask.TaskType.AFTERHOURS_FEE)
        t2 = flag_afterhours_fee(leg, time(22, 30))
        self.assertIsNone(t2)
        self.assertEqual(self._open_count(leg), 1)

    def test_flap_back_closes_open_task(self):
        res = self._res()
        leg = self._leg(res, pickup_time=time(20, 0))
        flag_afterhours_fee(leg, time(22, 30))
        self.assertEqual(self._open_count(leg), 1)
        # Flight moved back out of the window before charging → close the flag.
        flag_afterhours_fee(leg, time(14, 0))
        self.assertEqual(self._open_count(leg), 0)

    def test_already_applied_does_not_flag(self):
        res = self._res()
        leg = self._leg(res, pickup_time=time(23, 0), afterhours_fee=AFTERHOURS_FEE_AMOUNT)
        t = flag_afterhours_fee(leg, time(23, 0))
        self.assertIsNone(t)
        self.assertEqual(self._open_count(leg), 0)


class ChargeAfterhoursEndpointTests(_BillingFixtureMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staff_ah", is_staff=True)
        self.client.force_login(self.staff)
        self.customer.stripe_customer_id = "cus_test"
        self.customer.save(update_fields=["stripe_customer_id"])
        self.res = self._res(base=Decimal("100.00"))
        self.leg = self._leg(self.res, pickup_time=time(23, 0))

    @patch("dispatching.views._run_in_background", side_effect=lambda fn, *a, **k: None)
    @patch("dispatching.views.stripe.PaymentIntent.create")
    @patch("dispatching.views.stripe.PaymentMethod.list")
    def test_charge_applies_fee_and_closes_task(self, mock_list, mock_pi, _bg):
        from payment.models import Payment

        # Raise a flag first (leg not yet charged).
        flag_afterhours_fee(self.leg, time(23, 0))
        self.assertEqual(
            OperationalTask.objects.filter(
                leg=self.leg, task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).count(),
            1,
        )

        mock_list.return_value = Mock(data=[Mock(id="pm_1")])
        mock_pi.return_value = Mock(
            status="succeeded", id="pi_1", amount=2000, payment_method="pm_1"
        )

        before_total = self.res.total_price
        url = reverse("charge_afterhours_fee", kwargs={"leg_id": self.leg.id})
        resp = self.client.post(url, data=json.dumps({}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

        self.leg.refresh_from_db()
        self.res.refresh_from_db()
        self.assertEqual(self.leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(self.res.total_price, before_total + AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(
            Payment.objects.filter(reservation=self.res, amount=AFTERHOURS_FEE_AMOUNT).count(),
            1,
        )
        # Task closed.
        self.assertEqual(
            OperationalTask.objects.filter(
                leg=self.leg, task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).count(),
            0,
        )

    @patch("dispatching.views.stripe.PaymentMethod.list")
    def test_charge_rejected_when_no_card_on_file(self, mock_list):
        mock_list.return_value = Mock(data=[])
        url = reverse("charge_afterhours_fee", kwargs={"leg_id": self.leg.id})
        resp = self.client.post(url, data=json.dumps({}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.afterhours_fee, Decimal("0.00"))


class AfterhoursOutstandingTests(_BillingFixtureMixin, TestCase):
    """Leg.afterhours_fee_outstanding(): owed-but-not-charged, delay-aware."""

    def test_out_of_window_not_owed(self):
        leg = self._leg(self._res(), pickup_time=time(14, 0))
        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_in_window_pickup_not_charged_is_owed(self):
        leg = self._leg(self._res(), pickup_time=time(23, 0), afterhours_fee=Decimal("0.00"))
        self.assertEqual(leg.afterhours_fee_outstanding(), AFTERHOURS_FEE_AMOUNT)

    def test_in_window_already_charged_not_owed(self):
        # Booked late → marker already 20 → no flag (the user's requirement).
        leg = self._leg(self._res(), pickup_time=time(23, 0), afterhours_fee=AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))

    def test_delayed_flight_owed_before_pickup_matched(self):
        # Booked 8 PM (out of window) but the flight now arrives 10:30 PM — the
        # row should flag from the live flight time, before the pickup is matched.
        leg = self._leg(self._res(), pickup_date=date(2026, 6, 6), pickup_time=time(20, 0))
        arr = timezone.make_aware(
            datetime(2026, 6, 6, 22, 30), timezone.get_current_timezone()
        )
        leg.flight_information = Flight.objects.create(estimated_gate_arrival_local=arr)
        leg.save()
        leg.refresh_from_db()
        self.assertEqual(leg.get_trip_type(), "arrival")
        self.assertEqual(leg.afterhours_fee_outstanding(), AFTERHOURS_FEE_AMOUNT)

    def test_delayed_flight_already_charged_not_owed(self):
        leg = self._leg(
            self._res(), pickup_date=date(2026, 6, 6), pickup_time=time(20, 0),
            afterhours_fee=AFTERHOURS_FEE_AMOUNT,
        )
        arr = timezone.make_aware(
            datetime(2026, 6, 6, 22, 30), timezone.get_current_timezone()
        )
        leg.flight_information = Flight.objects.create(estimated_gate_arrival_local=arr)
        leg.save()
        leg.refresh_from_db()
        self.assertEqual(leg.afterhours_fee_outstanding(), Decimal("0.00"))


class ChargeAllAfterhoursTests(_BillingFixtureMixin, TestCase):
    """The semi-auto batch 'charge all' endpoint only touches owed legs."""

    def setUp(self):
        self.customer.stripe_customer_id = "cus_x"
        self.customer.save(update_fields=["stripe_customer_id"])
        self.staff = User.objects.create_user(username="bstaff", is_staff=True)
        self.client.force_login(self.staff)
        self.d = date(2026, 6, 6)
        # Two owed legs (in-window, not charged) + one already charged.
        self.owed1 = self._leg(self._res(base=Decimal("100")), pickup_date=self.d, pickup_time=time(23, 0))
        self.owed2 = self._leg(self._res(base=Decimal("100")), pickup_date=self.d, pickup_time=time(23, 30))
        self.charged = self._leg(
            self._res(base=Decimal("100")), pickup_date=self.d, pickup_time=time(23, 0),
            afterhours_fee=AFTERHOURS_FEE_AMOUNT,
        )

    @patch("dispatching.views._run_in_background", lambda *a, **k: None)
    @patch("dispatching.views.stripe.PaymentIntent.create")
    @patch("dispatching.views.stripe.PaymentMethod.list")
    def test_charge_all_charges_only_owed(self, mock_list, mock_pi):
        mock_list.return_value = Mock(data=[Mock(id="pm_1")])
        ids = iter(["pi_a", "pi_b", "pi_c"])
        mock_pi.side_effect = lambda **kw: Mock(
            status="succeeded", id=next(ids), amount=2000, payment_method="pm_1"
        )
        resp = self.client.post(
            reverse("charge_all_afterhours_fees"),
            data=json.dumps({"date": "2026-06-06"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["charged"], 2)
        self.assertEqual(body["total_owed"], 2)

        for leg in (self.owed1, self.owed2):
            leg.refresh_from_db()
            self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
            self.assertIn("After-Hours Fee charged", leg.private_notes or "")
        # The already-charged leg is untouched (no double-charge).
        self.charged.refresh_from_db()
        self.assertEqual(self.charged.afterhours_fee, AFTERHOURS_FEE_AMOUNT)
        self.assertNotIn("After-Hours Fee charged", self.charged.private_notes or "")


class PortalGratuityChargeTests(_BillingFixtureMixin, TestCase):
    """End-to-end portal charge: a whole-reservation gratuity splits the tip,
    notes each leg, and adds a reservation-level customer note."""

    def setUp(self):
        self.customer.stripe_customer_id = "cus_x"
        self.customer.save(update_fields=["stripe_customer_id"])
        self.staff = User.objects.create_user(username="pstaff", is_staff=True)
        self.client.force_login(self.staff)
        self.res = self._res(base=Decimal("230.00"))
        self.res.total_price = Decimal("250.00")
        self.res.save(update_fields=["total_price"])
        self.leg1, self.leg2 = self._leg(self.res), self._leg(self.res)

    @patch("dispatching.views._run_in_background", lambda *a, **k: None)
    @patch("dispatching.views.save_card_to_customer", lambda *a, **k: None)
    @patch("dispatching.views.stripe.PaymentIntent.create")
    @patch("dispatching.views.stripe.PaymentMethod.retrieve")
    @patch("dispatching.views.stripe.Customer.retrieve")
    @patch("dispatching.views.stripe.PaymentMethod.list")
    def test_whole_gratuity_charge_splits_notes_and_reservation_note(
        self, mock_list, mock_cust, mock_pm_ret, mock_pi
    ):
        pm = Mock()
        pm.type = "card"
        pm.customer = "cus_x"
        pm.card = Mock(brand="visa", last4="4242", exp_month=1, exp_year=2030)
        mock_list.return_value = Mock(data=[pm])
        mock_cust.return_value = Mock()
        mock_pm_ret.return_value = pm
        mock_pi.return_value = Mock(
            status="succeeded", id="pi_1", amount=4600, payment_method="pm_1"
        )

        url = reverse("dispatcher_payment_portal", kwargs={"reservation_id": self.res.uuid})
        resp = self.client.post(url, {
            "action": "use_saved_card",
            "payment_method_id": "pm_1",
            "amount": "46.00",
            "charge_type": "gratuity",
            "gratuity_target": "whole",
            "description": "Gratuity for Res",
        })
        self.assertIn(resp.status_code, (302, 200))

        self.res.refresh_from_db()
        self.assertEqual(self.res.gratuity_amount, Decimal("46.00"))
        self.assertEqual(self.res.total_price, Decimal("296.00"))  # 250 + 46
        self.assertIn("$46.00 Gratuity Included", self.res.special_requests or "")

        self.leg1.refresh_from_db()
        self.leg2.refresh_from_db()
        self.assertEqual(self.leg1.driver_gratuity, Decimal("23.00"))
        self.assertEqual(self.leg2.driver_gratuity, Decimal("23.00"))
        self.assertIn("$23.00 Gratuity Included", self.leg1.private_notes or "")
        self.assertIn("$23.00 Gratuity Included", self.leg2.private_notes or "")
