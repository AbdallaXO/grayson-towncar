"""
Fill the persistent route-distance cache (dispatching/route_distance.py) with real
Google Distance Matrix drive times for upcoming unknown-route legs.

This is the OFF-the-request-path resolver. The web render path only ever reads the
cache; this command is the one place the paid Distance Matrix call runs on the
scheduling side. Run it on a schedule:

    python manage.py resolve_route_distances

Deployment (web-only, no separate worker):
    Add a Railway *cron* service (or any external scheduler) that runs this command
    every ~10 minutes. It needs GOOGLE_MAPS_API_KEY in its environment. Cost is
    bounded — each distinct address pair is resolved once and refreshed every
    ROUTE_DISTANCE_REFRESH_DAYS days.

If a background schedulers process (run_schedulers) is ever added, this same logic
can be hung off it as an advisory-locked loop instead.
"""

from django.core.management.base import BaseCommand

from dispatching.route_distance import enqueue_upcoming_legs, resolve_pending

# Distinct advisory-lock id so two overlapping runs (cron fires while the last is
# still going) don't both scan/resolve. Mirrors the other schedulers' 737_20x ids.
_LOCK_ID = 737_204


def _try_lock():
    from django.db import connection
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", [_LOCK_ID])
        return cur.fetchone()[0]


def _unlock():
    from django.db import connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", [_LOCK_ID])


class Command(BaseCommand):
    help = "Precompute + resolve Google drive times for upcoming unknown-route legs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch", type=int, default=40,
            help="Max address pairs to resolve via Google this run (default 40).",
        )
        parser.add_argument(
            "--horizon-days", type=int, default=None,
            help="How many days ahead to precompute (default ROUTE_DISTANCE_HORIZON_DAYS).",
        )
        parser.add_argument(
            "--no-enqueue", action="store_true",
            help="Skip the upcoming-legs scan; only drain already-pending rows.",
        )

    def handle(self, *args, **opts):
        if not _try_lock():
            self.stdout.write("Another resolve_route_distances run holds the lock; exiting.")
            return
        try:
            enqueued = 0
            if not opts["no_enqueue"]:
                kwargs = {}
                if opts["horizon_days"] is not None:
                    kwargs["horizon_days"] = opts["horizon_days"]
                enqueued = enqueue_upcoming_legs(**kwargs)
                self.stdout.write(f"Enqueued {enqueued} new upcoming unknown-route pair(s).")

            resolved, failed, skipped = resolve_pending(batch=opts["batch"])
            self.stdout.write(self.style.SUCCESS(
                f"Resolved {resolved}, failed {failed}, deferred {skipped} "
                f"(enqueued {enqueued})."
            ))
        finally:
            _unlock()
