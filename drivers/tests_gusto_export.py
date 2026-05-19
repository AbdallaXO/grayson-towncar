"""Tests for the Gusto Smart Import CSV export.

Run with:  ./manage.py test drivers.tests_gusto_export

These tests deliberately stub `_min_pickup`/`_max_pickup` on DriverPayment
instances (instead of building real Reservation/Leg/Rate fixtures) so they
stay isolated and fast. There is one DB-backed test (`EligibleQuerysetTests`)
that exercises the actual queryset against a real Leg/LegPayment fixture.
"""
from datetime import date, timedelta
from decimal import Decimal
import io
import csv

from django.contrib.auth.models import User
from django.test import TestCase, RequestFactory
from django.urls import reverse

from drivers.models import Driver, DriverPayment, DriverPaymentExport
from drivers.gusto_export import (
    GUSTO_CSV_HEADER,
    GustoRow,
    _resolve_names,
    _format_ssn_ein,
    _format_amount,
    _parse_name,
    build_row,
    write_csv,
    csv_filename,
    validate_selection,
)


def _make_driver(username, *, first="", last="", driver_type="inhouse", is_active=True, **gusto_fields):
    """Lightweight Driver builder — no Reservation/Leg setup needed."""
    user = User.objects.create_user(
        username=username, first_name=first, last_name=last, is_active=is_active,
    )
    return Driver.objects.create(profile=user, driver_type=driver_type, **gusto_fields)


def _make_payment(driver, amount, *, min_pickup, max_pickup):
    """Create a DriverPayment and stub the leg-date annotations."""
    p = DriverPayment.objects.create(
        driver=driver, amount=Decimal(str(amount)),
    )
    # Set the annotations build_row checks. This dodges the heavy Reservation/Leg setup.
    p._min_pickup = min_pickup
    p._max_pickup = max_pickup
    return p


# ── Pure helpers (no DB) ──────────────────────────────────────────────

class ParseNameTests(TestCase):
    def test_two_words(self):
        self.assertEqual(_parse_name("Yovanny Suarez"), ("Yovanny", "Suarez"))

    def test_three_words_last_word_is_last_name(self):
        self.assertEqual(_parse_name("Jose Luis Garcia"), ("Jose Luis", "Garcia"))

    def test_one_word_only(self):
        self.assertEqual(_parse_name("Madonna"), ("Madonna", ""))

    def test_empty(self):
        self.assertEqual(_parse_name(""), ("", ""))
        self.assertEqual(_parse_name("   "), ("", ""))


class FormatAmountTests(TestCase):
    def test_two_decimals(self):
        self.assertEqual(_format_amount(Decimal("425")), "425.00")
        self.assertEqual(_format_amount(Decimal("425.5")), "425.50")
        self.assertEqual(_format_amount(Decimal("425.499")), "425.50")

    def test_none(self):
        self.assertEqual(_format_amount(None), "")


class FormatSsnEinTests(TestCase):
    def test_masked_already(self):
        d = _make_driver("d1", gusto_ssn_ein_last4="*9579")
        self.assertEqual(_format_ssn_ein(d), "*9579")

    def test_bare_digits_get_masked(self):
        d = _make_driver("d2", gusto_ssn_ein_last4="9579")
        self.assertEqual(_format_ssn_ein(d), "*9579")

    def test_falls_back_to_contractor_id(self):
        d = _make_driver("d3", gusto_contractor_id="abc123")
        self.assertEqual(_format_ssn_ein(d), "abc123")

    def test_empty(self):
        d = _make_driver("d4")
        self.assertEqual(_format_ssn_ein(d), "")


class ResolveNamesTests(TestCase):
    def test_gusto_overrides_take_priority(self):
        d = _make_driver(
            "d5", first="Jane", last="DoeProfile",
            gusto_first_name="Janet", gusto_last_name="DoeGusto",
        )
        f, l, b = _resolve_names(d)
        self.assertEqual((f, l, b), ("Janet", "DoeGusto", ""))

    def test_fallback_to_profile(self):
        d = _make_driver("d6", first="Bob", last="Roberts")
        f, l, b = _resolve_names(d)
        self.assertEqual((f, l, b), ("Bob", "Roberts", ""))

    def test_business_pass_through_clears_first_last(self):
        """When gusto_business_name is set, first/last MUST be blank so
        Gusto's matcher doesn't try to match the row as an individual."""
        d = _make_driver(
            "d7", first="Acme", last="LLC", gusto_business_name="Acme Transport LLC",
        )
        f, l, b = _resolve_names(d)
        self.assertEqual((f, l, b), ("", "", "Acme Transport LLC"))


# ── CSV writing ───────────────────────────────────────────────────────

class CSVWriteTests(TestCase):
    def _read_csv(self, rows):
        """Strip the leading UTF-8 BOM before parsing (Excel / Gusto do
        the same — the BOM is metadata, not a cell value)."""
        buf = io.StringIO()
        write_csv(rows, buf)
        content = buf.getvalue()
        if content.startswith("﻿"):
            content = content[1:]
        return list(csv.reader(io.StringIO(content)))

    def test_csv_starts_with_utf8_bom(self):
        """Gusto's Smart Import requires the BOM to recognize the header row."""
        buf = io.StringIO()
        write_csv([], buf)
        self.assertTrue(
            buf.getvalue().startswith("﻿"),
            "Gusto rejects CSVs without a UTF-8 BOM with 'Missing required CSV headers'.",
        )

    def test_header_is_exact(self):
        out = self._read_csv([])
        self.assertEqual(out, [GUSTO_CSV_HEADER])

    def test_individual_driver_row(self):
        row = GustoRow(
            payment=None,  # not used by write_csv
            contractor_type="Individual",
            last_name="Suarez", first_name="Yovanny",
            business_name="", ssn_ein="*9579",
            fixed_amount=Decimal("425.00"),
            invoice_number="736",
            note="Grayson Towncar driver payment 2026-05-11 to 2026-05-17",
        )
        out = self._read_csv([row])
        self.assertEqual(len(out), 2)  # header + 1
        self.assertEqual(out[0], GUSTO_CSV_HEADER)
        header = out[0]
        data = out[1]
        self.assertEqual(data[header.index("contractor_type")], "Individual")
        self.assertEqual(data[header.index("first_name")], "Yovanny")
        self.assertEqual(data[header.index("last_name")], "Suarez")
        self.assertEqual(data[header.index("business_name")], "")
        self.assertEqual(data[header.index("ssn")], "*9579")
        self.assertEqual(data[header.index("ein")], "",
                         "Individuals must NOT populate ein.")
        self.assertEqual(data[header.index("wage")], "425.00")
        self.assertEqual(data[header.index("invoice_number")], "736")
        self.assertEqual(data[header.index("memo")],
                         "Grayson Towncar driver payment 2026-05-11 to 2026-05-17")

    def test_business_contractor_row_uses_ein_not_ssn(self):
        row = GustoRow(
            payment=None,
            contractor_type="Business",
            last_name="", first_name="",
            business_name="Acme Transport LLC", ssn_ein="*1234",
            fixed_amount=Decimal("1200.00"),
            invoice_number="737",
            note="Grayson Towncar driver payment 2026-05-11 to 2026-05-17",
        )
        out = self._read_csv([row])
        header = out[0]
        data = out[1]
        self.assertEqual(data[header.index("contractor_type")], "Business")
        self.assertEqual(data[header.index("business_name")], "Acme Transport LLC")
        self.assertEqual(data[header.index("first_name")], "")
        self.assertEqual(data[header.index("last_name")], "")
        self.assertEqual(data[header.index("ssn")], "",
                         "Business rows must NOT populate ssn.")
        self.assertEqual(data[header.index("ein")], "*1234")
        self.assertEqual(data[header.index("wage")], "1200.00")
        self.assertEqual(data[header.index("invoice_number")], "737")

    def test_only_wage_carries_money_other_pay_cols_blank(self):
        row = GustoRow(
            payment=None, contractor_type="Individual",
            last_name="X", first_name="Y", business_name="",
            ssn_ein="*0001", fixed_amount=Decimal("100.00"),
            invoice_number="1", note="n",
        )
        out = self._read_csv([row])
        header = out[0]
        data = out[1]
        blank_pay_cols = ["hours_worked", "bonus", "tips", "cash_tips", "reimbursement"]
        for col in blank_pay_cols:
            idx = header.index(col)
            self.assertEqual(data[idx], "", f"Column {col} must be blank, got {data[idx]!r}")
        self.assertEqual(data[header.index("wage")], "100.00")


class CsvFilenameTests(TestCase):
    def test_uses_period_dates(self):
        self.assertEqual(
            csv_filename(date(2026, 5, 11), date(2026, 5, 17)),
            "gusto_contractor_payments_2026-05-11_to_2026-05-17.csv",
        )


# ── build_row blocker / warning logic ─────────────────────────────────

class BuildRowTests(TestCase):
    def setUp(self):
        self.from_date = date(2026, 5, 11)
        self.to_date = date(2026, 5, 17)

    def test_inhouse_with_valid_data_is_eligible(self):
        d = _make_driver("vh1", first="Yovanny", last="Suarez", gusto_ssn_ein_last4="9579")
        p = _make_payment(d, 425, min_pickup=date(2026, 5, 12), max_pickup=date(2026, 5, 16))
        row = build_row(p, self.from_date, self.to_date)
        self.assertTrue(row.is_eligible, msg=f"blockers: {row.blockers}")
        self.assertEqual(row.warnings, [])
        self.assertEqual(row.fixed_amount, Decimal("425"))
        self.assertEqual(row.ssn_ein, "*9579")
        self.assertEqual(row.note,
                         "Grayson Towncar driver payment 2026-05-11 to 2026-05-17")

    def test_affiliate_driver_is_blocked(self):
        d = _make_driver("af1", first="Aff", last="Iliate", driver_type="affiliate")
        p = _make_payment(d, 300, min_pickup=self.from_date, max_pickup=self.to_date)
        row = build_row(p, self.from_date, self.to_date)
        self.assertFalse(row.is_eligible)
        self.assertTrue(any("Affiliate" in b for b in row.blockers))

    def test_zero_amount_is_blocked(self):
        d = _make_driver("z1", first="Zero", last="Pay")
        p = _make_payment(d, 0, min_pickup=self.from_date, max_pickup=self.to_date)
        row = build_row(p, self.from_date, self.to_date)
        self.assertFalse(row.is_eligible)
        self.assertTrue(any("$0" in b or "negative" in b for b in row.blockers))

    def test_leg_before_period_is_blocked(self):
        d = _make_driver("lb1", first="Old", last="Leg")
        p = _make_payment(d, 100, min_pickup=date(2026, 5, 1), max_pickup=date(2026, 5, 14))
        row = build_row(p, self.from_date, self.to_date)
        self.assertFalse(row.is_eligible)
        self.assertTrue(any("before" in b for b in row.blockers))

    def test_leg_after_period_is_blocked(self):
        d = _make_driver("la1", first="New", last="Leg")
        p = _make_payment(d, 100, min_pickup=date(2026, 5, 12), max_pickup=date(2026, 5, 25))
        row = build_row(p, self.from_date, self.to_date)
        self.assertFalse(row.is_eligible)
        self.assertTrue(any("after" in b for b in row.blockers))

    def test_missing_gusto_identifier_is_warning_not_blocker(self):
        d = _make_driver("nw1", first="No", last="Identifier")  # no SSN, no contractor ID
        p = _make_payment(d, 200, min_pickup=self.from_date, max_pickup=self.to_date)
        row = build_row(p, self.from_date, self.to_date)
        self.assertTrue(row.is_eligible, msg=f"unexpected blockers: {row.blockers}")
        self.assertTrue(any("Gusto identifier" in w for w in row.warnings))

    def test_missing_legal_name_is_warning(self):
        # No first/last on profile, no business name set
        d = _make_driver("nn1")
        p = _make_payment(d, 200, min_pickup=self.from_date, max_pickup=self.to_date)
        row = build_row(p, self.from_date, self.to_date)
        self.assertTrue(any("legal first/last name" in w or "Missing legal" in w for w in row.warnings))

    def test_inactive_driver_with_payment_is_still_eligible(self):
        """Inactive drivers should still appear if they have a processed payment in the period."""
        d = _make_driver("ia1", first="Inactive", last="Driver", is_active=False)
        p = _make_payment(d, 250, min_pickup=self.from_date, max_pickup=self.to_date)
        row = build_row(p, self.from_date, self.to_date)
        self.assertTrue(row.is_eligible)


# ── validate_selection (defense against tampered submissions) ─────────

class ValidateSelectionTests(TestCase):
    def setUp(self):
        self.from_date = date(2026, 5, 11)
        self.to_date = date(2026, 5, 17)

    def test_invalid_id_produces_error(self):
        result = validate_selection(["abc", "12.5"], self.from_date, self.to_date)
        self.assertTrue(result.errors)
        self.assertFalse(result.valid_payments)

    def test_missing_id_produces_error(self):
        result = validate_selection([99999999], self.from_date, self.to_date)
        self.assertTrue(any("not found" in e for e in result.errors))

    def test_affiliate_payment_is_blocked(self):
        d = _make_driver("af-sel", first="A", last="B", driver_type="affiliate")
        p = DriverPayment.objects.create(driver=d, amount=Decimal("100"))
        result = validate_selection([p.id], self.from_date, self.to_date)
        self.assertFalse(result.valid_payments)
        self.assertIn(p.id, result.skipped_ids)
        self.assertTrue(any("Affiliate" in e for e in result.errors))

    def test_zero_amount_payment_is_blocked(self):
        d = _make_driver("z-sel", first="A", last="B")
        p = DriverPayment.objects.create(driver=d, amount=Decimal("0"))
        result = validate_selection([p.id], self.from_date, self.to_date)
        self.assertFalse(result.valid_payments)
        self.assertTrue(any("$0" in e or "negative" in e for e in result.errors))


# ── eligible_payments_qs against a real Leg fixture ───────────────────

class EligibleQuerysetTests(TestCase):
    """One DB-backed test exercising the actual queryset filter.

    Builds the minimal stack: Vehicle → Location(s) → Route → Rate →
    Customer → Reservation → Leg → DriverPayment + LegPayment.
    """

    @classmethod
    def setUpTestData(cls):
        from rates.models import Vehicle, Location, Route, Rate
        from reservations.models import Customer, Reservation, Leg
        from drivers.models import LegPayment

        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4,
        )
        cls.origin = Location.objects.create(name="MCO")
        cls.dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(origin=cls.origin, destination=cls.dest)
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Cust", last_name="One", email="c1@example.com", phone_number="555",
        )
        cls.reservation = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, rate=cls.rate, vehicle=cls.vehicle,
            base_price=Decimal("100"), total_price=Decimal("100"),
        )

        cls.drv_in = _make_driver("ih_real", first="Inhouse", last="Real")
        cls.drv_aff = _make_driver("af_real", first="Aff", last="Real", driver_type="affiliate")

        # In-period payment for in-house driver
        cls.pay_in_period = DriverPayment.objects.create(
            driver=cls.drv_in, amount=Decimal("425.00"),
        )
        leg = Leg.objects.create(
            reservation=cls.reservation, driver=cls.drv_in,
            pickup_date=date(2026, 5, 13), pickup_time="09:00",
            pickup_location="MCO", dropoff_location="Disney",
            payment_status="paid", driver_pay_amount=Decimal("425.00"),
        )
        LegPayment.objects.create(
            payment=cls.pay_in_period, leg=leg, amount=Decimal("425.00"),
        )

        # Out-of-period payment for the same in-house driver
        cls.pay_out_period = DriverPayment.objects.create(
            driver=cls.drv_in, amount=Decimal("300.00"),
        )
        old_leg = Leg.objects.create(
            reservation=cls.reservation, driver=cls.drv_in,
            pickup_date=date(2026, 4, 20), pickup_time="09:00",
            pickup_location="MCO", dropoff_location="Disney",
            payment_status="paid", driver_pay_amount=Decimal("300.00"),
        )
        LegPayment.objects.create(
            payment=cls.pay_out_period, leg=old_leg, amount=Decimal("300.00"),
        )

        # In-period payment for AFFILIATE driver — should be excluded
        cls.pay_affiliate = DriverPayment.objects.create(
            driver=cls.drv_aff, amount=Decimal("250.00"),
        )
        aff_leg = Leg.objects.create(
            reservation=cls.reservation, driver=cls.drv_aff,
            pickup_date=date(2026, 5, 14), pickup_time="09:00",
            pickup_location="MCO", dropoff_location="Disney",
            payment_status="paid", driver_pay_amount=Decimal("250.00"),
        )
        LegPayment.objects.create(
            payment=cls.pay_affiliate, leg=aff_leg, amount=Decimal("250.00"),
        )

    def test_returns_only_inhouse_in_period(self):
        from drivers.gusto_export import eligible_payments_qs
        ids = set(eligible_payments_qs(date(2026, 5, 11), date(2026, 5, 17)).values_list("id", flat=True))
        self.assertIn(self.pay_in_period.id, ids)
        self.assertNotIn(self.pay_out_period.id, ids,
                         "Payment with legs outside the period must not appear.")
        self.assertNotIn(self.pay_affiliate.id, ids,
                         "Affiliate payments must not appear.")

    def test_active_driver_with_no_payment_in_period_is_excluded(self):
        # Driver with no payments at all
        ghost = _make_driver("ghost", first="Ghost", last="Driver")
        from drivers.gusto_export import eligible_payments_qs
        ids = set(eligible_payments_qs(date(2026, 5, 11), date(2026, 5, 17)).values_list("id", flat=True))
        # No payment exists for this driver, so nothing of theirs appears.
        self.assertFalse(
            any(p.driver_id == ghost.id for p in DriverPayment.objects.filter(id__in=ids))
        )


# ── View-level permission + flow ──────────────────────────────────────

class GustoExportViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="staffer", password="x", is_staff=True)
        cls.non_staff = User.objects.create_user(username="random", password="x", is_staff=False)
        cls.driver = _make_driver(
            "view_drv", first="View", last="Driver", gusto_ssn_ein_last4="0001",
        )

    def test_non_staff_redirected(self):
        self.client.login(username="random", password="x")
        resp = self.client.get(reverse("gusto_export"))
        self.assertEqual(resp.status_code, 302)

    def test_staff_can_load_page(self):
        self.client.login(username="staffer", password="x")
        resp = self.client.get(reverse("gusto_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Gusto Payroll CSV Export")

    def test_post_with_no_selection_redirects_with_error(self):
        self.client.login(username="staffer", password="x")
        resp = self.client.post(reverse("gusto_export"), {
            "from_date": "2026-05-11",
            "to_date": "2026-05-17",
        })
        self.assertEqual(resp.status_code, 302)

    def test_post_with_invalid_payment_id_does_not_download(self):
        self.client.login(username="staffer", password="x")
        resp = self.client.post(reverse("gusto_export"), {
            "from_date": "2026-05-11",
            "to_date": "2026-05-17",
            "payment_ids": ["abc", "9999999"],
        })
        # We refuse to stream a CSV when validation fails
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("text/csv", resp.get("Content-Type", ""))

    def test_post_blocks_non_staff_with_redirect(self):
        self.client.login(username="random", password="x")
        resp = self.client.post(reverse("gusto_export"), {
            "from_date": "2026-05-11",
            "to_date": "2026-05-17",
            "payment_ids": ["1"],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("text/csv", resp.get("Content-Type", ""))


# ── DriverPaymentExport audit row ─────────────────────────────────────

class ExportAuditTests(TestCase):
    def test_audit_row_records_basics(self):
        user = User.objects.create_user(username="auditor", password="x", is_staff=True)
        rec = DriverPaymentExport.objects.create(
            created_by=user,
            from_date=date(2026, 5, 11),
            to_date=date(2026, 5, 17),
            csv_file_name="gusto_contractor_payments_2026-05-11_to_2026-05-17.csv",
            selected_driver_count=2,
            total_amount=Decimal("815.00"),
            exported_payment_ids=[101, 102],
        )
        self.assertEqual(rec.exported_payment_ids, [101, 102])
        self.assertEqual(rec.selected_driver_count, 2)
