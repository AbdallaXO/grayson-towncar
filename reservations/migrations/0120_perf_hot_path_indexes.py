"""Hot-path composite indexes for the slow list endpoints (incident 2026-07-18).

- Leg(driver, status)        -> /drivers/completed-trips/ (driver + status='completed')
- Leg(pickup_date, status)   -> dispatch board / date views (pickup_date + status)
- Reservation(status, created_at) -> /dispatching/reservations-list/ default
  (exclude cancelled, order by -created_at within the 90-day window)

Plain AddIndex (works on both SQLite dev and Postgres). On Postgres this takes a
brief write lock while the index builds; the tables here are modest so it is
seconds. If they ever grow large, switch these to
django.contrib.postgres.operations.AddIndexConcurrently (with atomic = False).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0119_flight_departure_date_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="leg",
            index=models.Index(fields=["driver", "status"], name="leg_driver_status_idx"),
        ),
        migrations.AddIndex(
            model_name="leg",
            index=models.Index(fields=["pickup_date", "status"], name="leg_pickup_status_idx"),
        ),
        migrations.AddIndex(
            model_name="reservation",
            index=models.Index(fields=["status", "created_at"], name="res_status_created_idx"),
        ),
    ]
