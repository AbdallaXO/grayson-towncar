"""
Data migration: reclassify every existing reservation's booking_source under the
current channel rules, so historical bookings pick up the channels added in 0116
(Bing, ChatGPT, Gemini, Perplexity, ...) instead of sitting in "direct".

Runs automatically on deploy so the founder doesn't have to run a command or click
the in-app "Reclassify sources" button. Idempotent — a second run changes nothing.
Phone bookings are left alone (no request context to re-tag them; they'd wrongly
collapse to "direct").
"""
from django.db import migrations


def reclassify_sources(apps, schema_editor):
    # derive_booking_source is pure (reads only field attributes + classify_channel),
    # so it's safe to call on historical-state model instances here.
    from reservations.attribution import derive_booking_source

    Reservation = apps.get_model("reservations", "Reservation")
    qs = Reservation.objects.exclude(booking_source="phone").only(
        "id", "booking_source", "gclid", "fbclid",
        "utm_source", "utm_medium", "referrer_host", "travel_agent",
    )
    changed = []
    for r in qs.iterator(chunk_size=2000):
        new_source = derive_booking_source(r, request=None)
        if new_source != r.booking_source:
            r.booking_source = new_source
            changed.append(r)
    if changed:
        Reservation.objects.bulk_update(changed, ["booking_source"], batch_size=1000)


def noop(apps, schema_editor):
    # No meaningful reverse — the old coarse tags can't be reconstructed.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0116_historicalreservation_referrer_host_and_more"),
    ]

    operations = [
        migrations.RunPython(reclassify_sources, noop),
    ]
