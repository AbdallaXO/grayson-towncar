"""
Payment signals — keep Reservation paid-state columns in sync.

Reservation.is_paid / paid_amount / gross_paid / total_refunded / first_paid_at
are the columns the revenue KPI dashboard queries against. They're maintained
here on every Payment save/delete so the dashboard never has to compute paid
status in Python.
"""
from decimal import Decimal
import logging

from django.db.models import Sum
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver

from payment.models import Payment

logger = logging.getLogger(__name__)


def _recompute_reservation_paid_state(reservation):
    """
    Recompute is_paid / paid_amount / gross_paid / total_refunded / first_paid_at
    for a single Reservation by aggregating its Payments. Uses a direct
    .update() so we don't trigger Reservation.save() (which has its own
    pricing/commission side effects we don't want to re-run on every payment).
    """
    if reservation is None:
        return

    # Local import to avoid app-loading circularity
    from reservations.models import Reservation

    Reservation.objects.filter(pk=reservation.pk).update(
        **compute_paid_state(reservation)
    )


def compute_paid_state(reservation) -> dict:
    """
    Pure helper: derive the target paid-state columns for a reservation by
    aggregating its Payment rows. Returned dict maps 1:1 to the Reservation
    columns. Shared by the live signal and the backfill command so the two
    can never diverge.
    """
    paid_qs = reservation.payments.filter(status="paid")
    gross = paid_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    refunded = paid_qs.aggregate(s=Sum("refunded_amount"))["s"] or Decimal("0.00")
    net = (gross - refunded).quantize(Decimal("0.01"))

    first_paid_at = (
        paid_qs.order_by("created_at").values_list("created_at", flat=True).first()
    )

    return {
        "is_paid": net > 0,
        "paid_amount": net,
        "gross_paid": gross.quantize(Decimal("0.01")),
        "total_refunded": refunded.quantize(Decimal("0.01")),
        "first_paid_at": first_paid_at,
    }


@receiver(post_save, sender=Payment)
def _payment_saved(sender, instance, **kwargs):
    try:
        _recompute_reservation_paid_state(instance.reservation)
    except Exception as exc:
        logger.exception("Failed to sync reservation paid state on Payment save: %s", exc)


@receiver(post_delete, sender=Payment)
def _payment_deleted(sender, instance, **kwargs):
    try:
        _recompute_reservation_paid_state(instance.reservation)
    except Exception as exc:
        logger.exception("Failed to sync reservation paid state on Payment delete: %s", exc)


PAID_STATE_FIELDS = ("is_paid", "paid_amount", "gross_paid", "total_refunded", "first_paid_at")


def _reservation_pre_save(sender, instance, update_fields=None, **kwargs):
    """The paid-state columns are owned by the Payment rows, never by whoever
    is holding a Reservation instance.

    Found 2026-09-21: the Stripe webhook does ``payment.save()`` (this module
    sets is_paid=True through an UPDATE) and then ``reservation.save()`` on the
    instance it loaded before the payment — which writes is_paid=False straight
    back. 2,406 of the 2,415 reservations paid since 1 September were flipped
    back within ten seconds of paying. Any full save from any stale instance
    does the same. So before a Reservation row is written, its paid-state
    columns are recomputed from its payments, and the instance's own values
    are ignored. Saves that name update_fields without any of these columns
    are left alone: Django will not write them anyway.
    """
    if instance.pk is None:
        return
    if update_fields is not None and not (set(update_fields) & set(PAID_STATE_FIELDS)):
        return
    try:
        for name, value in compute_paid_state(instance).items():
            setattr(instance, name, value)
    except Exception as exc:
        logger.exception("Could not refresh paid state before saving reservation %s: %s", instance.pk, exc)


def _connect_reservation_pre_save():
    from reservations.models import Reservation

    pre_save.connect(
        _reservation_pre_save, sender=Reservation,
        dispatch_uid="payment.signals.reservation_paid_state_pre_save",
    )


_connect_reservation_pre_save()
