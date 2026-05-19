"""
Gusto Smart Import CSV export for processed in-house driver payments.

This module is a thin export layer that runs AFTER the existing driver
payment processing flow (see `dispatching.views.process_driver_payment`).
It does NOT calculate, mutate, or re-process any payment data — it only
reads finalized `DriverPayment` records and emits the row format Gusto
accepts on its Smart Import contractor-payment upload screen.

Hard rules (mirror the spec):
  - The contractor's total payment goes into `fixed_amount`. No other
    amount column is populated.
  - Only in-house drivers with an actual processed `DriverPayment` for
    the period are included.
  - Affiliate drivers are excluded.
  - Payments with amount <= 0 are excluded.
  - We never touch Gusto's API and never mark anything as paid.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from django.db.models import Max, Min, Q

from drivers.models import DriverPayment


# Exact Gusto Smart Import header. Order and spelling must not change —
# the staff workflow is "click upload, Gusto auto-maps columns by name".
GUSTO_CSV_HEADER = [
    "last_name",
    "first_name",
    "business_name",
    "ssn/ein",
    "hourly_rate",
    "hours",
    "fixed_amount",
    "bonus",
    "reimbursement",
    "tips",
    "cash_tips",
    "invoice_number",
    "note",
]


@dataclass
class GustoRow:
    """One CSV row plus the metadata the UI needs to render eligibility."""
    payment: DriverPayment
    last_name: str
    first_name: str
    business_name: str
    ssn_ein: str
    fixed_amount: Decimal
    note: str
    warnings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    @property
    def is_eligible(self) -> bool:
        return not self.blockers


def _parse_name(full_name: str) -> tuple[str, str]:
    """Split a one-string display name into (first, last) — last word is last name."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _resolve_names(driver) -> tuple[str, str, str]:
    """Return (first_name, last_name, business_name) using Gusto overrides → profile → parse fallback."""
    business = (driver.gusto_business_name or "").strip()

    first = (driver.gusto_first_name or "").strip()
    last = (driver.gusto_last_name or "").strip()

    profile = getattr(driver, "profile", None)
    if not first and profile is not None:
        first = (profile.first_name or "").strip()
    if not last and profile is not None:
        last = (profile.last_name or "").strip()

    # Last-resort: parse a single display string. Only used when nothing else exists.
    if not first and not last and profile is not None:
        full = profile.get_full_name() or profile.username or ""
        first, last = _parse_name(full)

    return first, last, business


def _format_ssn_ein(driver) -> str:
    """Build the masked identifier Gusto matches contractors by.

    Priority:
      1) driver.gusto_ssn_ein_last4 (mask with leading * if 4 bare digits)
      2) driver.gusto_contractor_id
      3) empty string (Gusto will try to match by name)
    """
    last4 = (driver.gusto_ssn_ein_last4 or "").strip()
    if last4:
        if last4.startswith("*"):
            return last4
        # If staff entered "1452", normalize to "*1452" so Gusto recognizes it.
        digits = "".join(ch for ch in last4 if ch.isdigit())
        if digits:
            return f"*{digits[-4:]}"
        return last4
    cid = (driver.gusto_contractor_id or "").strip()
    if cid:
        return cid
    return ""


def _format_amount(amount) -> str:
    """Two-decimal string. Empty string for None — but callers should pre-filter."""
    if amount is None:
        return ""
    return f"{Decimal(amount).quantize(Decimal('0.01'))}"


def build_row(payment: DriverPayment, from_date: date, to_date: date) -> GustoRow:
    """Map one processed DriverPayment to a Gusto CSV row + UI warnings/blockers.

    Blockers are hard reasons to refuse export (affiliate, zero/negative
    amount, leg outside period). Warnings are advisory — missing legal
    name pieces or missing matching identifier.
    """
    driver = payment.driver
    first, last, business = _resolve_names(driver)
    ssn_ein = _format_ssn_ein(driver)
    amount = Decimal(payment.amount or 0)

    note = f"Grayson Towncar driver payment {from_date.isoformat()} to {to_date.isoformat()}"

    row = GustoRow(
        payment=payment,
        last_name=last,
        first_name=first,
        business_name=business,
        ssn_ein=ssn_ein,
        fixed_amount=amount,
        note=note,
    )

    # ── Hard blockers ──
    if driver.driver_type != "inhouse":
        row.blockers.append("Affiliate driver — excluded from Gusto contractor payroll")
    if amount <= 0:
        row.blockers.append("Payment amount is $0 or negative")

    # Detect legs outside the requested period. We use prefetched data if the
    # caller annotated it (see iter_eligible_payments) to avoid an extra query.
    min_pickup = getattr(payment, "_min_pickup", None)
    max_pickup = getattr(payment, "_max_pickup", None)
    if min_pickup is None or max_pickup is None:
        # Only consider ACTIVE lines — voided lines are excluded from the
        # period filter so the export reflects what was actually paid.
        agg = payment.leg_payments.filter(status="active").aggregate(
            mn=Min("leg__pickup_date"), mx=Max("leg__pickup_date"),
        )
        min_pickup = agg["mn"]
        max_pickup = agg["mx"]
    if min_pickup and min_pickup < from_date:
        row.blockers.append(
            f"Includes legs before {from_date.isoformat()} (oldest leg: {min_pickup.isoformat()})"
        )
    if max_pickup and max_pickup > to_date:
        row.blockers.append(
            f"Includes legs after {to_date.isoformat()} (newest leg: {max_pickup.isoformat()})"
        )

    # ── Soft warnings ──
    has_individual_name = bool(first and last)
    if not has_individual_name and not business:
        row.warnings.append("Missing legal first/last name and business name")
    elif not has_individual_name and business:
        # Business-only contractors are fine for Gusto, just call it out.
        row.warnings.append("Exporting as business (no individual legal name)")
    if not ssn_ein:
        row.warnings.append("No Gusto identifier (SSN/EIN last 4 or contractor ID) — Gusto will match by name")

    return row


def eligible_payments_qs(from_date: date, to_date: date):
    """Processed in-house DriverPayments whose ACTIVE legs all fall in [from_date, to_date].

    Voided LegPayment lines are ignored for period detection — so once
    staff void a wrong-period leg via the statement detail page, the
    payment automatically becomes eligible for the prior period's CSV.

    Returns a queryset annotated with `_min_pickup` and `_max_pickup`
    (over active lines only) for use by `build_row`.
    """
    from django.db.models import Q

    active = Q(leg_payments__status="active")
    return (
        DriverPayment.objects
        .select_related("driver", "driver__profile", "created_by")
        .filter(
            driver__driver_type="inhouse",
            amount__gt=0,
            leg_payments__status="active",
            leg_payments__leg__pickup_date__gte=from_date,
            leg_payments__leg__pickup_date__lte=to_date,
        )
        .annotate(
            _min_pickup=Min("leg_payments__leg__pickup_date", filter=active),
            _max_pickup=Max("leg_payments__leg__pickup_date", filter=active),
        )
        .filter(_min_pickup__gte=from_date, _max_pickup__lte=to_date)
        .distinct()
        .order_by("driver__profile__last_name", "driver__profile__first_name", "id")
    )


def build_rows_for_period(from_date: date, to_date: date) -> list[GustoRow]:
    """Convenience: queryset → list[GustoRow] for the UI."""
    return [build_row(p, from_date, to_date) for p in eligible_payments_qs(from_date, to_date)]


def write_csv(rows: Iterable[GustoRow], out) -> None:
    """Write the Gusto-compatible CSV to `out` (a text-mode file/StringIO).

    Only `fixed_amount` is populated for amounts — all other amount columns
    (hourly_rate, hours, bonus, reimbursement, tips, cash_tips) stay blank.
    """
    writer = csv.writer(out)
    writer.writerow(GUSTO_CSV_HEADER)
    for r in rows:
        writer.writerow([
            r.last_name,            # last_name
            r.first_name,           # first_name
            r.business_name,        # business_name
            r.ssn_ein,              # ssn/ein
            "",                     # hourly_rate — INTENTIONALLY BLANK
            "",                     # hours — INTENTIONALLY BLANK
            _format_amount(r.fixed_amount),  # fixed_amount — total payment
            "",                     # bonus — INTENTIONALLY BLANK
            "",                     # reimbursement — INTENTIONALLY BLANK
            "",                     # tips — INTENTIONALLY BLANK
            "",                     # cash_tips — INTENTIONALLY BLANK
            "",                     # invoice_number
            r.note,                 # note
        ])


def csv_filename(from_date: date, to_date: date) -> str:
    return f"gusto_contractor_payments_{from_date.isoformat()}_to_{to_date.isoformat()}.csv"


@dataclass
class ValidationResult:
    valid_payments: list  # list[DriverPayment]
    rows: list  # list[GustoRow] — built for the valid payments
    errors: list  # list[str] — hard-block reasons; UI shows + refuses export
    skipped_ids: list  # list[int] — payment IDs that were submitted but couldn't be exported


def validate_selection(
    payment_ids: Iterable,
    from_date: date,
    to_date: date,
) -> ValidationResult:
    """Hard-validate a staff-submitted list of payment IDs against the period.

    Hard-block reasons (per spec §7):
      - invalid / non-existent payment ID
      - affiliate driver
      - amount <= 0
      - any leg outside [from_date, to_date]

    Soft warnings (missing Gusto identifier, etc.) ride along on each row
    but do not block export.
    """
    # Sanitize incoming IDs — staff submit them via checkboxes but we still
    # validate to defend against tampered/forged form payloads.
    clean_ids: list[int] = []
    bad_ids: list = []
    for raw in payment_ids:
        try:
            clean_ids.append(int(raw))
        except (TypeError, ValueError):
            bad_ids.append(raw)

    errors: list[str] = []
    if bad_ids:
        errors.append(f"Invalid payment ID(s) submitted: {bad_ids!r}")

    # Pull only the IDs we were given. We do NOT pre-filter on
    # driver_type/amount here — we want explicit blockers if staff submitted
    # an affiliate/zero payment, not a silent drop.
    active = Q(leg_payments__status="active")
    qs = (
        DriverPayment.objects
        .select_related("driver", "driver__profile")
        .filter(id__in=clean_ids)
        .annotate(
            # Aggregate over ACTIVE lines only — voided lines shouldn't
            # extend the apparent period of a payment.
            _min_pickup=Min("leg_payments__leg__pickup_date", filter=active),
            _max_pickup=Max("leg_payments__leg__pickup_date", filter=active),
        )
    )
    found_ids = {p.id for p in qs}
    missing = [pid for pid in clean_ids if pid not in found_ids]
    if missing:
        errors.append(f"Payment ID(s) not found: {missing}")

    valid_payments = []
    rows: list[GustoRow] = []
    skipped_ids: list[int] = []
    for p in qs:
        row = build_row(p, from_date, to_date)
        if row.blockers:
            errors.append(
                f"Payment #{p.id} ({p.driver}): " + "; ".join(row.blockers)
            )
            skipped_ids.append(p.id)
            continue
        valid_payments.append(p)
        rows.append(row)

    return ValidationResult(
        valid_payments=valid_payments,
        rows=rows,
        errors=errors,
        skipped_ids=skipped_ids,
    )
