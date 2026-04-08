"""
Add booking_source / is_repeat_booking / persisted paid-state fields to
Reservation, and backfill them from existing data.

After this migration:
  - revenue KPIs can use .filter(is_paid=True) instead of looping over the
    Python @cached_property payment_status
  - source attribution dashboards can GROUP BY booking_source directly
"""
from decimal import Decimal

from django.db import migrations, models


def _backfill_attribution_and_paid(apps, schema_editor):
    Reservation = apps.get_model("reservations", "Reservation")
    Payment = apps.get_model("payment", "Payment")

    # We can't import reservations.attribution.derive_booking_source here
    # because it imports the live model. Re-implement the same logic against
    # the historical model. Keep this in sync with reservations/attribution.py.
    META_TOKENS = {"facebook", "fb", "ig", "instagram", "meta"}
    PAID_MEDIUM = {"cpc", "paid", "ads", "ppc", "paidsocial", "paid_social"}

    def derive(res):
        if res.travel_agent_id:
            return "travel_agent"
        src = (res.utm_source or "").strip().lower()
        med = (res.utm_medium or "").strip().lower()
        if res.gclid:
            return "google_ads"
        if res.fbclid or src in META_TOKENS:
            return "meta_ads" if med in PAID_MEDIUM else "meta_organic"
        if "google" in src:
            return "google_ads" if med in PAID_MEDIUM else "google_organic"
        if med == "referral" or src == "referral":
            return "referral"
        return "direct"

    # 1. Backfill booking_source
    seen_emails = set()
    repeat_pks = set()
    # Build a sorted pass to compute is_repeat_booking deterministically
    # (a reservation is "repeat" if its customer email appeared earlier).
    for res in (
        Reservation.objects.select_related("customer")
        .order_by("created_at", "id")
        .iterator()
    ):
        new_source = derive(res)
        is_repeat = False
        if res.customer_id and res.customer.email:
            email = res.customer.email.strip().lower()
            if email:
                if email in seen_emails:
                    is_repeat = True
                seen_emails.add(email)
        if is_repeat:
            repeat_pks.add(res.pk)
        Reservation.objects.filter(pk=res.pk).update(
            booking_source=new_source,
            is_repeat_booking=is_repeat,
        )

    # 2. Backfill paid state from existing Payments. Aggregate per reservation
    # to avoid loading every Payment into Python.
    from django.db.models import Sum, Min

    paid_agg = (
        Payment.objects.filter(status="paid", reservation__isnull=False)
        .values("reservation_id")
        .annotate(
            gross=Sum("amount"),
            refunded=Sum("refunded_amount"),
            first_at=Min("created_at"),
        )
    )
    for row in paid_agg:
        gross = row["gross"] or Decimal("0.00")
        refunded = row["refunded"] or Decimal("0.00")
        net = (gross - refunded).quantize(Decimal("0.01"))
        Reservation.objects.filter(pk=row["reservation_id"]).update(
            is_paid=net > 0,
            gross_paid=gross.quantize(Decimal("0.01")),
            total_refunded=refunded.quantize(Decimal("0.01")),
            paid_amount=net,
            first_paid_at=row["first_at"],
        )


def _noop_reverse(apps, schema_editor):
    # Field removal handled by AddField reverse; nothing to undo for data.
    pass


class Migration(migrations.Migration):

    # Postgres can't CREATE INDEX in the same transaction as an AddField that
    # backfills a default on a large table — the UPDATE leaves pending trigger
    # events that block the index creation. Disabling atomic lets each
    # operation commit independently. Safe here because the RunPython backfill
    # is idempotent.
    atomic = False

    dependencies = [
        ("reservations", "0092_merge_20260324_1547"),
        ("payment", "0014_payment_refunded_amount_payment_stripe_refund_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="booking_source",
            field=models.CharField(
                choices=[
                    ("google_ads", "Google Ads"),
                    ("google_organic", "Google Organic"),
                    ("meta_ads", "Meta Ads"),
                    ("meta_organic", "Meta Organic"),
                    ("travel_agent", "Travel Agent"),
                    ("referral", "Referral"),
                    ("direct", "Direct"),
                    ("phone", "Phone / Dispatcher"),
                    ("other", "Other"),
                ],
                db_index=True,
                default="direct",
                help_text="Normalized acquisition channel for KPI reporting",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="is_repeat_booking",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="True if this customer's email has a prior reservation. Independent of booking_source.",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="is_paid",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="True when at least one Payment with status='paid' exists (net of refunds > 0)",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="paid_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Net collected revenue (paid payments minus refunded amounts)",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="gross_paid",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Sum of paid Payment amounts before refunds",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="total_refunded",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Sum of refunded_amount across all paid payments",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="first_paid_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="Timestamp of the first successful payment — used for revenue trends",
                null=True,
            ),
        ),
        # Mirror the new fields onto the simple_history shadow model so future
        # historical snapshots include them. Existing historical rows will have
        # NULL/default values for these columns; that's expected.
        migrations.AddField(
            model_name="historicalreservation",
            name="booking_source",
            field=models.CharField(
                choices=[
                    ("google_ads", "Google Ads"),
                    ("google_organic", "Google Organic"),
                    ("meta_ads", "Meta Ads"),
                    ("meta_organic", "Meta Organic"),
                    ("travel_agent", "Travel Agent"),
                    ("referral", "Referral"),
                    ("direct", "Direct"),
                    ("phone", "Phone / Dispatcher"),
                    ("other", "Other"),
                ],
                db_index=True,
                default="direct",
                help_text="Normalized acquisition channel for KPI reporting",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="historicalreservation",
            name="is_repeat_booking",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="True if this customer's email has a prior reservation. Independent of booking_source.",
            ),
        ),
        migrations.AddField(
            model_name="historicalreservation",
            name="is_paid",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="True when at least one Payment with status='paid' exists (net of refunds > 0)",
            ),
        ),
        migrations.AddField(
            model_name="historicalreservation",
            name="paid_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Net collected revenue (paid payments minus refunded amounts)",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="historicalreservation",
            name="gross_paid",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Sum of paid Payment amounts before refunds",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="historicalreservation",
            name="total_refunded",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Sum of refunded_amount across all paid payments",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="historicalreservation",
            name="first_paid_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="Timestamp of the first successful payment — used for revenue trends",
                null=True,
            ),
        ),
        migrations.RunPython(_backfill_attribution_and_paid, _noop_reverse),
    ]
