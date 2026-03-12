"""
One-time data migration: link converted leads to their matching reservations
for accurate revenue attribution in analytics.
"""
from django.db import migrations


def backfill_converted_reservations(apps, schema_editor):
    Lead = apps.get_model("reservations", "Lead")
    Reservation = apps.get_model("reservations", "Reservation")

    unlinked = Lead.objects.filter(converted=True, converted_reservation__isnull=True)

    for lead in unlinked.iterator():
        matching = None

        # 1) Match by email
        if lead.email:
            matching = (
                Reservation.objects.filter(customer__email__iexact=lead.email)
                .order_by("-pickup_date")
                .first()
            )

        # 2) Match by phone — exact
        if not matching and lead.phone:
            matching = (
                Reservation.objects.filter(customer__phone_number__iexact=lead.phone)
                .order_by("-pickup_date")
                .first()
            )

        # 3) Match by phone — digit normalization (last 10 digits)
        if not matching and lead.phone:
            lead_digits = "".join(filter(str.isdigit, lead.phone))
            if len(lead_digits) >= 10:
                lead_last10 = lead_digits[-10:]
                last4 = lead_last10[-4:]
                candidates = (
                    Reservation.objects.filter(customer__phone_number__contains=last4)
                    .select_related("customer")
                    .order_by("-pickup_date")
                )
                for res in candidates:
                    cand_digits = "".join(filter(str.isdigit, res.customer.phone_number))
                    if len(cand_digits) >= 10 and cand_digits[-10:] == lead_last10:
                        matching = res
                        break

        if matching:
            lead.converted_reservation = matching
            lead.save(update_fields=["converted_reservation"])


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0085_add_initial_email_sent_to_lead"),
    ]

    operations = [
        migrations.RunPython(
            backfill_converted_reservations,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
