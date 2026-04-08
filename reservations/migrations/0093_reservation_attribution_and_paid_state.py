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
    """
    Bulk backfill — runs in a handful of SQL statements rather than
    one-UPDATE-per-row. Designed to finish during a release-phase migration
    on a Postgres table with tens of thousands of reservations.

    Skips entirely on non-Postgres backends (e.g. local sqlite tests),
    falling back to the row-by-row path that already works there.
    """
    vendor = schema_editor.connection.vendor

    if vendor != "postgresql":
        _backfill_rowbyrow(apps)
        return

    with schema_editor.connection.cursor() as cur:
        # 1. booking_source — single UPDATE with a CASE expression that
        # mirrors reservations.attribution.derive_booking_source().
        # Order matters: most specific signal wins.
        cur.execute(
            """
            UPDATE reservations_reservation
               SET booking_source = CASE
                   WHEN travel_agent_id IS NOT NULL THEN 'travel_agent'
                   WHEN gclid IS NOT NULL AND gclid <> '' THEN 'google_ads'
                   WHEN fbclid IS NOT NULL AND fbclid <> '' THEN
                        CASE WHEN LOWER(COALESCE(utm_medium,'')) IN
                                  ('cpc','paid','ads','ppc','paidsocial','paid_social')
                             THEN 'meta_ads' ELSE 'meta_organic' END
                   WHEN LOWER(COALESCE(utm_source,'')) IN
                        ('facebook','fb','ig','instagram','meta') THEN
                        CASE WHEN LOWER(COALESCE(utm_medium,'')) IN
                                  ('cpc','paid','ads','ppc','paidsocial','paid_social')
                             THEN 'meta_ads' ELSE 'meta_organic' END
                   WHEN LOWER(COALESCE(utm_source,'')) LIKE '%google%' THEN
                        CASE WHEN LOWER(COALESCE(utm_medium,'')) IN
                                  ('cpc','paid','ads','ppc','paidsocial','paid_social')
                             THEN 'google_ads' ELSE 'google_organic' END
                   WHEN LOWER(COALESCE(utm_medium,'')) = 'referral'
                     OR LOWER(COALESCE(utm_source,'')) = 'referral' THEN 'referral'
                   ELSE 'direct'
               END
            """
        )

        # 2. is_repeat_booking — flag any reservation whose customer's email
        # appears on an earlier reservation. Single UPDATE with EXISTS.
        cur.execute(
            """
            UPDATE reservations_reservation r
               SET is_repeat_booking = TRUE
              FROM reservations_customer c
             WHERE r.customer_id = c.id
               AND c.email <> ''
               AND EXISTS (
                   SELECT 1
                     FROM reservations_reservation r2
                     JOIN reservations_customer c2 ON c2.id = r2.customer_id
                    WHERE LOWER(c2.email) = LOWER(c.email)
                      AND r2.created_at < r.created_at
               )
            """
        )

        # 3. Paid state — aggregate Payments per reservation in a single
        # statement and update all matching reservations at once.
        cur.execute(
            """
            UPDATE reservations_reservation r
               SET gross_paid    = agg.gross,
                   total_refunded = agg.refunded,
                   paid_amount   = agg.net,
                   is_paid       = (agg.net > 0),
                   first_paid_at = agg.first_at
              FROM (
                  SELECT reservation_id,
                         COALESCE(SUM(amount), 0)          AS gross,
                         COALESCE(SUM(refunded_amount), 0) AS refunded,
                         COALESCE(SUM(amount), 0)
                           - COALESCE(SUM(refunded_amount), 0) AS net,
                         MIN(created_at)                   AS first_at
                    FROM payment_payment
                   WHERE status = 'paid'
                     AND reservation_id IS NOT NULL
                   GROUP BY reservation_id
              ) AS agg
             WHERE r.id = agg.reservation_id
            """
        )


def _backfill_rowbyrow(apps):
    """Slow but portable fallback used by sqlite (local dev/tests)."""
    Reservation = apps.get_model("reservations", "Reservation")
    Payment = apps.get_model("payment", "Payment")

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

    seen_emails = set()
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
        Reservation.objects.filter(pk=res.pk).update(
            booking_source=new_source,
            is_repeat_booking=is_repeat,
        )

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
