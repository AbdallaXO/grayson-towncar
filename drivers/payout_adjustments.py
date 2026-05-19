"""
Transactional helpers for correcting processed driver payment statements.

These functions are the ONLY safe entry points for mutating a finalized
DriverPayment + its LegPayment lines after `DriverPayment.create_payment`
has run. Each one:
  - validates inputs (reason required, amount non-negative, etc.)
  - runs inside `transaction.atomic()`
  - updates the line + DriverPayment.amount atomically
  - syncs Leg.payment_status so the unpaid queue stays the source of truth
  - writes one DriverPayoutAdjustment audit row

If you need to bypass these helpers, you are about to silently break
the audit trail. Don't. Edit Leg fields BEFORE processing (via
update_driver_pay_amount) or go through here AFTER.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from drivers.models import (
    DriverPayment,
    DriverPayoutAdjustment,
    LegPayment,
)


_MIN_REASON_LEN = 3


def _validate_reason(reason: str) -> str:
    """Trim + validate. Raises ValidationError on empty/too-short reason."""
    cleaned = (reason or "").strip()
    if len(cleaned) < _MIN_REASON_LEN:
        raise ValidationError(
            f"A reason of at least {_MIN_REASON_LEN} characters is required."
        )
    return cleaned


def _coerce_amount(value, *, field_name="amount") -> Decimal:
    """Convert to Decimal, raise ValidationError for bad / negative input."""
    if value is None or value == "":
        raise ValidationError(f"{field_name} is required.")
    try:
        amt = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError(f"{field_name} must be a number.")
    if amt < 0:
        raise ValidationError(f"{field_name} cannot be negative.")
    return amt.quantize(Decimal("0.01"))


def _statement_status_snapshot(payment: DriverPayment) -> tuple[bool, bool]:
    """Returns (was_emailed, was_exported) — used to stamp the audit row."""
    try:
        from ops.models import EmailLog
        emailed = EmailLog.objects.filter(
            email_type="driver_statement",
            success=True,
            metadata__payment_id=payment.id,
        ).exists()
        if not emailed:
            # EmailLog metadata sometimes stores ids as strings.
            emailed = EmailLog.objects.filter(
                email_type="driver_statement",
                success=True,
                metadata__payment_id=str(payment.id),
            ).exists()
    except Exception:
        emailed = False

    try:
        from drivers.models import DriverPaymentExport
        exported = DriverPaymentExport.objects.filter(
            exported_payment_ids__contains=payment.id,
        ).exists()
    except Exception:
        exported = False

    return emailed, exported


def leg_is_paid_to_driver(leg) -> bool:
    """True iff this Leg has at least one ACTIVE LegPayment.

    This is the safer source of truth than `Leg.payment_status` — that
    flag is a denormalized cache we keep in sync, but the active payout
    line is what actually represents "money was credited to the driver
    for this leg".
    """
    return LegPayment.objects.filter(leg=leg, status=LegPayment.STATUS_ACTIVE).exists()


def _resync_leg_paid_flag(leg) -> None:
    """Make `Leg.payment_status` match whether any active LegPayment exists."""
    target = "paid" if leg_is_paid_to_driver(leg) else "unpaid"
    if leg.payment_status != target:
        leg.payment_status = target
        leg.save(update_fields=["payment_status"])


def _recalculate_payment_total(payment: DriverPayment) -> None:
    """Set DriverPayment.amount = sum of active line amounts.

    Also recomputes base_pay / gratuity / additional rollups so the
    statement card on the detail page stays consistent.
    """
    active = payment.leg_payments.filter(status=LegPayment.STATUS_ACTIVE)
    total = Decimal("0.00")
    base = Decimal("0.00")
    grat = Decimal("0.00")
    addl = Decimal("0.00")
    for lp in active:
        total += Decimal(lp.amount or 0)
        base += Decimal(lp.base_pay or 0)
        grat += Decimal(lp.gratuity or 0)
        addl += Decimal(lp.additional or 0)
    payment.amount = total.quantize(Decimal("0.01"))
    # Only persist sub-totals when at least one active line had them set;
    # otherwise leave the original aggregated values alone (they're
    # informational on the email/statement card).
    payment.base_pay = base.quantize(Decimal("0.01")) if base > 0 else None
    payment.gratuity = grat.quantize(Decimal("0.01")) if grat > 0 else None
    payment.additional = addl.quantize(Decimal("0.01")) if addl > 0 else None
    payment.save(
        update_fields=["amount", "base_pay", "gratuity", "additional"]
    )


# ── Public operations ────────────────────────────────────────────────


def void_leg_payment(
    leg_payment: LegPayment, *, user, reason: str,
) -> DriverPayoutAdjustment:
    """Void an active LegPayment line.

    - status flips to "voided"
    - voided_at / voided_by / void_reason captured
    - DriverPayment.amount recalculated (active lines only)
    - Leg.payment_status returns to "unpaid" if no other active line
      covers it (defensive — current data model only has one line per
      leg per payment, but the rule is right)
    - One DriverPayoutAdjustment row written

    Idempotent: re-voiding an already-voided line is a no-op that
    raises ValidationError (so staff get explicit feedback rather than
    silently doing nothing).
    """
    cleaned_reason = _validate_reason(reason)

    with transaction.atomic():
        # Lock the row to avoid two staff voiding simultaneously.
        lp = (
            LegPayment.objects
            .select_for_update()
            .select_related("leg", "payment")
            .get(pk=leg_payment.pk)
        )
        if lp.status == LegPayment.STATUS_VOIDED:
            raise ValidationError("This line is already voided.")

        payment = lp.payment
        old_amount = Decimal(lp.amount or 0)
        delta = -old_amount  # voiding subtracts from the payment total
        was_emailed, was_exported = _statement_status_snapshot(payment)

        lp.status = LegPayment.STATUS_VOIDED
        lp.voided_at = timezone.now()
        lp.voided_by = user
        lp.void_reason = cleaned_reason
        lp.updated_by = user
        lp.save(update_fields=[
            "status", "voided_at", "voided_by", "void_reason",
            "updated_at", "updated_by",
        ])

        _recalculate_payment_total(payment)
        if lp.leg:
            _resync_leg_paid_flag(lp.leg)

        return DriverPayoutAdjustment.objects.create(
            payment=payment,
            leg_payment=lp,
            leg=lp.leg,
            adjustment_type=DriverPayoutAdjustment.TYPE_VOID,
            old_amount=old_amount,
            new_amount=None,
            delta=delta,
            reason=cleaned_reason,
            created_by=user,
            statement_was_emailed=was_emailed,
            statement_was_exported=was_exported,
        )


def edit_leg_payment_amount(
    leg_payment: LegPayment, *, new_amount, user, reason: str,
) -> DriverPayoutAdjustment:
    """Update the amount on an active LegPayment line.

    - captures `original_amount` on first edit (preserves the at-process
      value across multiple corrections)
    - puts the new total into `amount` (base/gratuity/additional are
      left alone — we don't try to split the delta into pay components)
    - recalculates DriverPayment.amount
    - writes a DriverPayoutAdjustment row showing old → new + delta

    Raises ValidationError if the line is voided, the new amount is
    negative / non-numeric, or the reason is too short.
    """
    cleaned_reason = _validate_reason(reason)
    coerced_amount = _coerce_amount(new_amount, field_name="New amount")

    with transaction.atomic():
        lp = (
            LegPayment.objects
            .select_for_update()
            .select_related("leg", "payment")
            .get(pk=leg_payment.pk)
        )
        if lp.status != LegPayment.STATUS_ACTIVE:
            raise ValidationError(
                "Cannot edit a voided line. Restore it first, or add a "
                "new line with the correct amount."
            )

        payment = lp.payment
        old_amount = Decimal(lp.amount or 0)
        if coerced_amount == old_amount:
            raise ValidationError(
                "New amount is the same as the current amount — nothing to change."
            )

        delta = coerced_amount - old_amount
        was_emailed, was_exported = _statement_status_snapshot(payment)

        if lp.original_amount is None:
            lp.original_amount = old_amount
        lp.amount = coerced_amount
        lp.updated_by = user
        lp.save(update_fields=["amount", "original_amount", "updated_at", "updated_by"])

        _recalculate_payment_total(payment)

        return DriverPayoutAdjustment.objects.create(
            payment=payment,
            leg_payment=lp,
            leg=lp.leg,
            adjustment_type=DriverPayoutAdjustment.TYPE_EDIT,
            old_amount=old_amount,
            new_amount=coerced_amount,
            delta=delta,
            reason=cleaned_reason,
            created_by=user,
            statement_was_emailed=was_emailed,
            statement_was_exported=was_exported,
        )


def add_missing_leg_to_payment(
    payment: DriverPayment, *, leg, amount, user, reason: str,
) -> DriverPayoutAdjustment:
    """Add a new active LegPayment for a previously-unpaid leg.

    Validates:
      - leg belongs to the same driver as the payment
      - leg.status == "completed"
      - leg.payment_status == "unpaid" (no active LegPayment exists for it)
      - amount >= 0
      - reason non-blank

    Side effects:
      - new LegPayment row created with status="active"
      - Leg.payment_status flipped to "paid"
      - DriverPayment.amount recalculated
      - DriverPayoutAdjustment row written (old_amount=None,
        new_amount=amount, delta=+amount)

    Raises ValidationError on any rule violation.
    """
    cleaned_reason = _validate_reason(reason)
    coerced_amount = _coerce_amount(amount, field_name="Amount")

    if leg.driver_id != payment.driver_id:
        raise ValidationError(
            "This leg is assigned to a different driver — it cannot be "
            "added to this payment."
        )
    if leg.status != "completed":
        raise ValidationError(
            "Only completed legs can be added to a payment."
        )
    if leg_is_paid_to_driver(leg):
        raise ValidationError(
            "This leg already has an active payment line. Void it on the "
            "other statement first if you need to move it here."
        )

    with transaction.atomic():
        # Lock the payment for the duration to make the total recompute consistent.
        locked_payment = (
            DriverPayment.objects
            .select_for_update()
            .get(pk=payment.pk)
        )
        was_emailed, was_exported = _statement_status_snapshot(locked_payment)

        # An existing voided LegPayment for this (payment, leg) pair would
        # collide with the unique_together constraint. If one exists,
        # resurrect it instead of creating a new row.
        existing = (
            LegPayment.objects
            .filter(payment=locked_payment, leg=leg)
            .select_for_update()
            .first()
        )
        if existing:
            existing.status = LegPayment.STATUS_ACTIVE
            existing.amount = coerced_amount
            existing.voided_at = None
            existing.voided_by = None
            existing.void_reason = ""
            existing.updated_by = user
            existing.save(update_fields=[
                "status", "amount", "voided_at", "voided_by",
                "void_reason", "updated_at", "updated_by",
            ])
            lp = existing
        else:
            lp = LegPayment.objects.create(
                payment=locked_payment,
                leg=leg,
                amount=coerced_amount,
                # base/gratuity/additional intentionally left null —
                # staff entered a single total. They can edit later if
                # they want to split it.
                base_pay=None,
                gratuity=None,
                additional=None,
                status=LegPayment.STATUS_ACTIVE,
                updated_by=user,
            )

        _recalculate_payment_total(locked_payment)
        _resync_leg_paid_flag(leg)

        return DriverPayoutAdjustment.objects.create(
            payment=locked_payment,
            leg_payment=lp,
            leg=leg,
            adjustment_type=DriverPayoutAdjustment.TYPE_ADD,
            old_amount=None,
            new_amount=coerced_amount,
            delta=coerced_amount,
            reason=cleaned_reason,
            created_by=user,
            statement_was_emailed=was_emailed,
            statement_was_exported=was_exported,
        )


def statement_email_status(payment: DriverPayment) -> dict:
    """For UI banners — has this statement been emailed / exported yet?

    Returns a dict with bools + the most recent timestamp for each, or
    None when that channel hasn't fired. Used to drive the amber
    "you've already shared this — re-send if needed" banner on the
    statement detail page.
    """
    emailed = False
    last_emailed_at = None
    try:
        from django.db.models import Q
        from ops.models import EmailLog
        emails = (
            EmailLog.objects
            .filter(email_type="driver_statement", success=True)
            .filter(
                Q(metadata__payment_id=payment.id)
                | Q(metadata__payment_id=str(payment.id))
            )
            .order_by("-sent_at")
            .values_list("sent_at", flat=True)
        )
        first = next(iter(emails), None)
        if first is not None:
            emailed = True
            last_emailed_at = first
    except Exception:
        pass

    exported = False
    last_exported_at = None
    try:
        from drivers.models import DriverPaymentExport
        exports = (
            DriverPaymentExport.objects
            .filter(exported_payment_ids__contains=payment.id)
            .order_by("-created_at")
            .values_list("created_at", flat=True)
        )
        first = next(iter(exports), None)
        if first is not None:
            exported = True
            last_exported_at = first
    except Exception:
        pass

    return {
        "emailed": emailed,
        "last_emailed_at": last_emailed_at,
        "exported": exported,
        "last_exported_at": last_exported_at,
    }
