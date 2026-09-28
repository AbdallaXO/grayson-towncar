"""Repair UUID columns in a local SQLite snapshot pulled from production.

Django stores a UUIDField on SQLite as 32 hex characters with NO dashes. Postgres
renders its native uuid type WITH dashes, and psycopg2 hands those back as plain
strings unless psycopg2.extras.register_uuid() has been called — which
scripts/pull_prod_snapshot.py did not do. Its adapt() has always had a
`uuid.UUID -> value.hex` branch, but that branch never fired, so every row landed
in the snapshot as a 36-character dashed string.

The visible symptom: every reservation detail page 404s locally. The URL carries
the uuid, Django normalises it to 32-char hex to query, and nothing matches.
Production is unaffected — Postgres holds the native type and compares correctly.

The transform is exactly reversible (it only removes the four dashes), and it
refuses to run against anything but SQLite.

Usage:
    python manage.py fix_snapshot_uuids --dry-run    # report, write nothing
    python manage.py fix_snapshot_uuids              # apply
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from reservations.models import Reservation


class Command(BaseCommand):
    help = "Rewrite dashed UUIDs in a local snapshot to the 32-char form Django expects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **opts):
        # Hard stop: this is a snapshot repair. Running it anywhere near the real
        # database would be rewriting primary identifiers for no reason.
        if connection.vendor != "sqlite":
            raise CommandError(
                f"Refusing to run against a {connection.vendor} database. "
                "This command only repairs a local SQLite snapshot."
            )

        with connection.cursor() as cur:
            cur.execute(
                "SELECT length(uuid), COUNT(*) FROM reservations_reservation "
                "GROUP BY length(uuid) ORDER BY 1"
            )
            lengths = cur.fetchall()

        total = Reservation.objects.count()
        broken = sum(n for ln, n in lengths if ln == 36)
        healthy = sum(n for ln, n in lengths if ln == 32)
        other = total - broken - healthy

        self.stdout.write(f"reservations: {total}")
        for ln, n in lengths:
            label = {32: "correct", 36: "dashed — Django cannot match these"}.get(ln, "unexpected")
            self.stdout.write(f"  uuid length {ln}: {n} rows  ({label})")

        if other:
            self.stdout.write(
                self.style.WARNING(f"\n{other} rows have an unexpected uuid length; leaving them alone.")
            )

        if not broken:
            self.stdout.write(self.style.SUCCESS("\nNothing to fix — every uuid is already in Django's form."))
            return

        # A collision here would mean two reservations genuinely share an id, which
        # the unique constraint would then reject halfway through. Check first.
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM (SELECT replace(uuid,'-','') AS u "
                "FROM reservations_reservation GROUP BY u HAVING COUNT(*) > 1)"
            )
            collisions = cur.fetchone()[0]
        if collisions:
            raise CommandError(
                f"{collisions} uuid collisions would result — refusing to write. "
                "Re-pull the snapshot instead."
            )

        self.stdout.write(f"\n{broken} rows would be rewritten (dashes removed).")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run: nothing written."))
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE reservations_reservation SET uuid = replace(uuid, '-', '') "
                    "WHERE length(uuid) = 36"
                )
                changed = cur.rowcount

        self.stdout.write(self.style.SUCCESS(f"Rewrote {changed} uuids. Reservation pages will open now."))
