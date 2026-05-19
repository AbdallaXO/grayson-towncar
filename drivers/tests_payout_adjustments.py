"""Tests for the driver payout adjustment workflow.

Run with:  ./manage.py test drivers.tests_payout_adjustments

These exercise the void / edit-amount / add-missing-leg helpers plus
the view-level permission and validation paths. They build a minimal
Reservation/Leg fixture per test class so each test starts with a clean
slate and a real DriverPayment + LegPayment.
"""
from datetime import date, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from drivers.models import (
    Driver,
    DriverPayment,
    DriverPayoutAdjustment,
    LegPayment,
)
from drivers.payout_adjustments import (
    add_missing_leg_to_payment,
    edit_leg_payment_amount,
    leg_is_paid_to_driver,
    statement_email_status,
    void_leg_payment,
)
from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg


def _make_driver(username, first="First", last="Last", driver_type="inhouse"):
    user = User.objects.create_user(username=username, first_name=first, last_name=last)
    return Driver.objects.create(profile=user, driver_type=driver_type)


def _bootstrap_fixtures():
    """Vehicle / Route / Rate / Customer / Reservation reused across tests."""
    vehicle = Vehicle.objects.create(vehicle_type="sedan", capacity=4, luggage_capacity=4)
    origin = Location.objects.create(name="MCO")
    dest = Location.objects.create(name="Disney")
    route = Route.objects.create(origin=origin, destination=dest)
    rate = Rate.objects.create(
        vehicle=vehicle, route=route,
        oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
    )
    customer = Customer.objects.create(
        first_name="C", last_name="One", email="c@example.com", phone_number="555",
    )
    reservation = Reservation.objects.create(
        trip_type="one-way", customer=customer, rate=rate, vehicle=vehicle,
        base_price=Decimal("100"), total_price=Decimal("100"),
    )
    return reservation


def _make_leg(reservation, driver, *, pickup_date, amount=Decimal("100.00"), status="completed"):
    leg = Leg.objects.create(
        reservation=reservation, driver=driver,
        pickup_date=pickup_date, pickup_time=time(9, 0),
        pickup_location="MCO", dropoff_location="Disney",
        status=status,
        payment_status="unpaid",
        driver_base_pay=amount,
        driver_pay_amount=amount,
    )
    return leg


def _process_payment(driver, legs):
    """Mimic the production flow: create DriverPayment + LegPayments + mark legs paid."""
    return DriverPayment.create_payment(driver=driver, legs=list(legs))


# ── void_leg_payment ──────────────────────────────────────────────────


class VoidLegPaymentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reservation = _bootstrap_fixtures()
        cls.driver = _make_driver("vlp")
        cls.staff = User.objects.create_user(username="staff_void", is_staff=True)

    def setUp(self):
        self.leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 13), amount=Decimal("100.00"))
        self.payment = _process_payment(self.driver, [self.leg])
        self.line = self.payment.leg_payments.get(leg=self.leg)

    def test_void_marks_voided_returns_leg_to_unpaid_and_decrements_total(self):
        before = self.payment.amount
        adj = void_leg_payment(self.line, user=self.staff, reason="Wrong period")

        self.line.refresh_from_db()
        self.payment.refresh_from_db()
        self.leg.refresh_from_db()

        self.assertEqual(self.line.status, "voided")
        self.assertEqual(self.line.void_reason, "Wrong period")
        self.assertEqual(self.line.voided_by, self.staff)
        self.assertIsNotNone(self.line.voided_at)

        self.assertEqual(self.payment.amount, before - Decimal("100.00"))
        self.assertEqual(self.leg.payment_status, "unpaid")

        self.assertEqual(adj.adjustment_type, "void_line")
        self.assertEqual(adj.old_amount, Decimal("100.00"))
        self.assertIsNone(adj.new_amount)
        self.assertEqual(adj.delta, Decimal("-100.00"))
        self.assertEqual(adj.reason, "Wrong period")
        self.assertEqual(adj.created_by, self.staff)

    def test_void_requires_reason(self):
        with self.assertRaises(ValidationError):
            void_leg_payment(self.line, user=self.staff, reason="")
        with self.assertRaises(ValidationError):
            void_leg_payment(self.line, user=self.staff, reason="  ")

    def test_re_voiding_is_blocked(self):
        void_leg_payment(self.line, user=self.staff, reason="First void")
        self.line.refresh_from_db()
        with self.assertRaises(ValidationError):
            void_leg_payment(self.line, user=self.staff, reason="Try again")

    def test_voided_line_is_not_paid_to_driver(self):
        self.assertTrue(leg_is_paid_to_driver(self.leg))
        void_leg_payment(self.line, user=self.staff, reason="Wrong leg")
        self.leg.refresh_from_db()
        self.assertFalse(leg_is_paid_to_driver(self.leg))


# ── edit_leg_payment_amount ───────────────────────────────────────────


class EditLegPaymentAmountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reservation = _bootstrap_fixtures()
        cls.driver = _make_driver("edit_lp")
        cls.staff = User.objects.create_user(username="staff_edit", is_staff=True)

    def setUp(self):
        self.leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 13), amount=Decimal("25.00"))
        self.payment = _process_payment(self.driver, [self.leg])
        self.line = self.payment.leg_payments.get(leg=self.leg)

    def test_edit_25_to_40_captures_original_and_recalcs_total(self):
        adj = edit_leg_payment_amount(
            self.line, new_amount="40", user=self.staff, reason="Pay rate was wrong",
        )
        self.line.refresh_from_db()
        self.payment.refresh_from_db()

        self.assertEqual(self.line.amount, Decimal("40.00"))
        self.assertEqual(self.line.original_amount, Decimal("25.00"))
        self.assertEqual(self.payment.amount, Decimal("40.00"))

        self.assertEqual(adj.adjustment_type, "edit_amount")
        self.assertEqual(adj.old_amount, Decimal("25.00"))
        self.assertEqual(adj.new_amount, Decimal("40.00"))
        self.assertEqual(adj.delta, Decimal("15.00"))

    def test_second_edit_does_not_overwrite_original_amount(self):
        edit_leg_payment_amount(
            self.line, new_amount="40", user=self.staff, reason="First fix",
        )
        edit_leg_payment_amount(
            self.line.__class__.objects.get(pk=self.line.pk),
            new_amount="50", user=self.staff, reason="Second fix",
        )
        self.line.refresh_from_db()
        self.assertEqual(self.line.amount, Decimal("50.00"))
        self.assertEqual(self.line.original_amount, Decimal("25.00"),
                         "original_amount must remain the at-process value")

    def test_edit_to_same_amount_is_rejected(self):
        with self.assertRaises(ValidationError):
            edit_leg_payment_amount(
                self.line, new_amount="25.00", user=self.staff, reason="No change",
            )

    def test_negative_amount_rejected(self):
        with self.assertRaises(ValidationError):
            edit_leg_payment_amount(
                self.line, new_amount="-10", user=self.staff, reason="Bad",
            )

    def test_blank_reason_rejected(self):
        with self.assertRaises(ValidationError):
            edit_leg_payment_amount(
                self.line, new_amount="40", user=self.staff, reason="",
            )

    def test_cannot_edit_voided_line(self):
        void_leg_payment(self.line, user=self.staff, reason="Void it first")
        self.line.refresh_from_db()
        with self.assertRaises(ValidationError):
            edit_leg_payment_amount(
                self.line, new_amount="40", user=self.staff, reason="Edit voided",
            )


# ── add_missing_leg_to_payment ────────────────────────────────────────


class AddMissingLegTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reservation = _bootstrap_fixtures()
        cls.driver = _make_driver("add_lp")
        cls.other_driver = _make_driver("other_drv", first="Other")
        cls.staff = User.objects.create_user(username="staff_add", is_staff=True)

    def setUp(self):
        self.existing_leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 13), amount=Decimal("100.00"))
        self.payment = _process_payment(self.driver, [self.existing_leg])

        # Missing leg — still unpaid, same driver
        self.missing_leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 14), amount=Decimal("75.00"))

    def test_add_missing_leg_creates_active_line_and_increments_total(self):
        before = self.payment.amount
        adj = add_missing_leg_to_payment(
            self.payment, leg=self.missing_leg, amount="75",
            user=self.staff, reason="Was missed during processing",
        )

        self.payment.refresh_from_db()
        self.missing_leg.refresh_from_db()
        self.assertEqual(self.payment.amount, before + Decimal("75.00"))
        self.assertEqual(self.missing_leg.payment_status, "paid")

        new_line = LegPayment.objects.get(payment=self.payment, leg=self.missing_leg)
        self.assertEqual(new_line.status, "active")
        self.assertEqual(new_line.amount, Decimal("75.00"))

        self.assertEqual(adj.adjustment_type, "add_missing_leg")
        self.assertIsNone(adj.old_amount)
        self.assertEqual(adj.new_amount, Decimal("75.00"))
        self.assertEqual(adj.delta, Decimal("75.00"))

    def test_cannot_add_paid_leg(self):
        # Process the missing_leg into its own payment first → now it's paid
        DriverPayment.create_payment(driver=self.driver, legs=[self.missing_leg])
        self.missing_leg.refresh_from_db()
        self.assertEqual(self.missing_leg.payment_status, "paid")

        with self.assertRaises(ValidationError):
            add_missing_leg_to_payment(
                self.payment, leg=self.missing_leg, amount="50",
                user=self.staff, reason="Try anyway",
            )

    def test_cannot_add_leg_belonging_to_different_driver(self):
        other_leg = _make_leg(self.reservation, self.other_driver, pickup_date=date(2026, 5, 14))
        with self.assertRaises(ValidationError):
            add_missing_leg_to_payment(
                self.payment, leg=other_leg, amount="50",
                user=self.staff, reason="Wrong driver",
            )

    def test_cannot_add_non_completed_leg(self):
        pending_leg = _make_leg(
            self.reservation, self.driver,
            pickup_date=date(2026, 5, 15), status="confirmed",
        )
        with self.assertRaises(ValidationError):
            add_missing_leg_to_payment(
                self.payment, leg=pending_leg, amount="50",
                user=self.staff, reason="Not completed",
            )

    def test_blank_reason_rejected(self):
        with self.assertRaises(ValidationError):
            add_missing_leg_to_payment(
                self.payment, leg=self.missing_leg, amount="50",
                user=self.staff, reason="",
            )

    def test_resurrects_existing_voided_line(self):
        """If a (payment, leg) pair was previously voided, adding the leg
        back re-uses that row (unique_together prevents a duplicate)."""
        # First add via processing — payment already has the existing_leg
        # Now void it, then re-add via add_missing_leg_to_payment.
        existing_line = self.payment.leg_payments.get(leg=self.existing_leg)
        void_leg_payment(existing_line, user=self.staff, reason="Will re-add")
        self.existing_leg.refresh_from_db()
        self.assertEqual(self.existing_leg.payment_status, "unpaid")

        adj = add_missing_leg_to_payment(
            self.payment, leg=self.existing_leg, amount="120",
            user=self.staff, reason="Correcting earlier void",
        )
        resurrected = LegPayment.objects.get(payment=self.payment, leg=self.existing_leg)
        self.assertEqual(resurrected.status, "active")
        self.assertEqual(resurrected.amount, Decimal("120.00"))
        self.assertEqual(adj.adjustment_type, "add_missing_leg")


# ── Gusto export integration ──────────────────────────────────────────


class GustoExportAfterVoidTests(TestCase):
    """Regression: voiding a wrong-period leg should make the payment
    eligible for the prior period's CSV export."""

    @classmethod
    def setUpTestData(cls):
        cls.reservation = _bootstrap_fixtures()
        cls.driver = _make_driver("gusto_void")
        cls.staff = User.objects.create_user(username="staff_gusto", is_staff=True)

    def test_voiding_wrong_period_leg_unblocks_validate_selection(self):
        """Mirrors the production blocker the user hit:
        payments #736/737/738 had a 5/18 leg in a 5/11-5/17 batch.
        After voiding that leg, the period validator should pass.
        """
        from drivers.gusto_export import validate_selection

        in_leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 13), amount=Decimal("100"))
        wrong_leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 18), amount=Decimal("50"))
        payment = _process_payment(self.driver, [in_leg, wrong_leg])

        # Before voiding: validate_selection refuses because the 5/18 leg
        # is outside the period.
        result_before = validate_selection(
            [payment.id], date(2026, 5, 11), date(2026, 5, 17),
        )
        self.assertEqual(result_before.valid_payments, [])
        self.assertTrue(
            any("after 2026-05-17" in e for e in result_before.errors),
            f"Expected out-of-period blocker, got: {result_before.errors!r}",
        )

        # Void the 5/18 leg via the actual production helper
        wrong_line = payment.leg_payments.get(leg=wrong_leg)
        void_leg_payment(wrong_line, user=self.staff, reason="Wrong period")

        # After voiding: validate_selection accepts, only the 5/13 leg
        # is active so the period filter sees only in-period dates.
        result_after = validate_selection(
            [payment.id], date(2026, 5, 11), date(2026, 5, 17),
        )
        self.assertEqual(
            [p.id for p in result_after.valid_payments], [payment.id],
            f"Expected payment {payment.id} to be valid; errors={result_after.errors!r}",
        )

        # And the corrected DriverPayment.amount should be 100, not 150.
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("100.00"))


# ── statement_email_status helper ─────────────────────────────────────


class StatementEmailStatusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reservation = _bootstrap_fixtures()
        cls.driver = _make_driver("ses")

    def test_emailed_flag_picks_up_emaillog_with_int_payment_id(self):
        leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 13))
        payment = _process_payment(self.driver, [leg])
        # Initially: not emailed
        status = statement_email_status(payment)
        self.assertFalse(status["emailed"])

        from ops.models import EmailLog
        EmailLog.objects.create(
            email_type="driver_statement",
            recipient_email="x@example.com",
            success=True,
            metadata={"driver_id": self.driver.id, "payment_id": payment.id},
        )
        status = statement_email_status(payment)
        self.assertTrue(status["emailed"])
        self.assertIsNotNone(status["last_emailed_at"])


# ── View-level permission + flow ──────────────────────────────────────


class AdjustmentViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reservation = _bootstrap_fixtures()
        cls.driver = _make_driver("adj_view")
        cls.staff = User.objects.create_user(username="adj_staff", password="x", is_staff=True)
        cls.non_staff = User.objects.create_user(username="adj_rand", password="x", is_staff=False)

    def setUp(self):
        self.leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 13), amount=Decimal("100.00"))
        self.payment = _process_payment(self.driver, [self.leg])
        self.line = self.payment.leg_payments.get(leg=self.leg)

    def _void_url(self):
        return reverse("void_leg_payment", args=[self.driver.id, self.payment.id, self.line.id])

    def _edit_url(self):
        return reverse("edit_leg_payment_amount", args=[self.driver.id, self.payment.id, self.line.id])

    def test_non_staff_cannot_void(self):
        self.client.login(username="adj_rand", password="x")
        resp = self.client.post(self._void_url(), {"reason": "should not work"})
        self.assertEqual(resp.status_code, 302)
        self.line.refresh_from_db()
        self.assertEqual(self.line.status, "active",
                         "Non-staff must not mutate the line.")
        self.assertEqual(DriverPayoutAdjustment.objects.count(), 0)

    def test_staff_can_void_with_reason(self):
        self.client.login(username="adj_staff", password="x")
        resp = self.client.post(self._void_url(), {"reason": "Out of period"})
        self.assertEqual(resp.status_code, 302)
        self.line.refresh_from_db()
        self.assertEqual(self.line.status, "voided")
        self.assertEqual(DriverPayoutAdjustment.objects.count(), 1)

    def test_void_with_blank_reason_redirects_with_no_mutation(self):
        self.client.login(username="adj_staff", password="x")
        resp = self.client.post(self._void_url(), {"reason": ""})
        self.assertEqual(resp.status_code, 302)
        self.line.refresh_from_db()
        self.assertEqual(self.line.status, "active")
        self.assertEqual(DriverPayoutAdjustment.objects.count(), 0)

    def test_edit_view_flow(self):
        self.client.login(username="adj_staff", password="x")
        resp = self.client.post(self._edit_url(), {
            "new_amount": "150",
            "reason": "Rate was wrong",
        })
        self.assertEqual(resp.status_code, 302)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, Decimal("150.00"))


# ── update_driver_pay_amount guard ────────────────────────────────────


class UpdateDriverPayAmountGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reservation = _bootstrap_fixtures()
        cls.driver = _make_driver("guard_drv")
        cls.staff = User.objects.create_user(username="guard_staff", password="x", is_staff=True)

    def test_cannot_edit_already_paid_leg_via_legacy_endpoint(self):
        leg = _make_leg(self.reservation, self.driver, pickup_date=date(2026, 5, 13), amount=Decimal("100"))
        _process_payment(self.driver, [leg])
        leg.refresh_from_db()
        self.assertEqual(leg.payment_status, "paid")

        self.client.login(username="guard_staff", password="x")
        import json
        resp = self.client.post(
            "/dispatching/update-driver-pay-amount/",
            data=json.dumps({
                "leg_id": leg.id,
                "driver_base_pay": 200,
                "driver_gratuity": 0,
                "driver_additional": 0,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        leg.refresh_from_db()
        # Leg pay amount must NOT have changed
        self.assertEqual(leg.driver_base_pay, Decimal("100.00"))
