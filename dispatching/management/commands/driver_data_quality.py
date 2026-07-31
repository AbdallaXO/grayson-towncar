"""
Driver status-discipline report — who produces trustworthy timing data.

WHY THIS EXISTS
---------------
Every "how long does this really take" number in the company is derived by
differencing LegStatus rows (there are no actual-time columns on Leg). That
makes the numbers only as good as the drivers' button discipline.

Auditing production (2026-02..2026-07) turned up two DIFFERENT failure modes
that look identical on a completion dashboard but need opposite responses:

  1. INSTANT-COMPLETE — the driver taps "Picked Up" and "Complete" within a
     couple of minutes of each other, i.e. hits both at the end of the trip.
     Their ride times are ~0 minutes. These drivers score 95-100% "full chain"
     and would rank as top performers on any compliance view, while
     contributing no usable timing data at all. This is the dangerous one:
     complete-looking data that is wrong.

  2. SPARSE — the driver simply doesn't run the full ladder often. Low
     full-chain %, but the trips they DO complete properly are perfectly good
     samples.

Only (1) should be excluded from timing. Excluding (2) would throw away real
data. The existing `exclude_from_timing` flag was found pointing at several
(2)-type drivers whose data is actually excellent, while not flagging a single
(1)-type driver.

Note that `MIN_PICKUP_TO_COMPLETE` in dispatching.analytics already rejects the
instant rides themselves, so aggregate medians are largely protected. What the
flag additionally buys you is honesty in driver-comparison views (where a
0-minute ride makes someone look impossibly fast) and a clear coaching list.

USAGE
-----
    python manage.py driver_data_quality                  # report only (default)
    python manage.py driver_data_quality --days 90
    python manage.py driver_data_quality --apply          # write exclude_from_timing
    python manage.py driver_data_quality --apply --yes    # skip confirmation

Dry-run is the default, matching the repo's other backfill commands.
"""

from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from dispatching.analytics import (
    first_status_times,
    MIN_PICKUP_TO_COMPLETE,
    REQUIRED_ANALYTICS_STATUSES,
)


# A driver whose completed trips are this often "instant" is tapping both
# buttons at once. Set well above the ~5-20% baseline that normal drivers show
# (a genuine short hop, or one honest double-tap, happens to everyone).
INSTANT_SHARE_EXCLUDE = 0.40

# Below this many observed legs we cannot fairly judge anyone.
MIN_LEGS_TO_JUDGE = 25

# Informational only — a low full-chain rate is a coaching note, NOT grounds
# for exclusion. Sparse data is still honest data.
SPARSE_FULL_CHAIN = 0.50


class Command(BaseCommand):
    help = "Report (and optionally apply) which drivers produce trustworthy timing data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=180,
            help="Look-back window in days (default: 180).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the recommended exclude_from_timing flags. Default is report-only.",
        )
        parser.add_argument(
            "--yes", action="store_true",
            help="Skip the confirmation prompt when using --apply.",
        )

    def handle(self, *args, **opts):
        from drivers.models import Driver
        from reservations.models import Leg

        days = opts["days"]
        cutoff = (timezone.now() - timedelta(days=days)).date()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nDriver status discipline — legs since {cutoff} ({days} days)\n"
        ))

        # Only in-house drivers matter: analytics already discards every
        # non-inhouse driver (see analytics.update_single_route_timing_metric),
        # so flagging an affiliate would be a no-op.
        legs = (
            Leg.objects
            .filter(pickup_date__gte=cutoff, driver__isnull=False,
                    driver__driver_type="inhouse")
            .select_related("driver", "driver__profile")
            .prefetch_related("status_history")
        )

        stats = defaultdict(lambda: {"legs": 0, "full": 0, "instant": 0, "rides": []})

        for leg in legs.iterator(chunk_size=500):
            times = first_status_times(leg, REQUIRED_ANALYTICS_STATUSES)
            if not times:
                continue
            s = stats[leg.driver_id]
            s["legs"] += 1
            if not REQUIRED_ANALYTICS_STATUSES.issubset(times.keys()):
                continue
            s["full"] += 1
            ride = (times["completed"] - times["picked-up"]).total_seconds() / 60
            if ride < MIN_PICKUP_TO_COMPLETE:
                s["instant"] += 1
            else:
                s["rides"].append(ride)

        drivers = {d.id: d for d in Driver.objects.select_related("profile")}

        rows = []
        for did, s in stats.items():
            driver = drivers.get(did)
            if not driver or s["legs"] < MIN_LEGS_TO_JUDGE:
                continue
            full_share = s["full"] / s["legs"]
            instant_share = s["instant"] / s["full"] if s["full"] else 0.0
            rides = sorted(s["rides"])
            median_ride = rides[len(rides) // 2] if rides else 0.0

            if instant_share >= INSTANT_SHARE_EXCLUDE:
                verdict = "EXCLUDE"
            elif full_share < SPARSE_FULL_CHAIN:
                verdict = "sparse"
            else:
                verdict = "good"

            rows.append({
                "driver": driver,
                "name": driver.profile.username if driver.profile else f"#{did}",
                "legs": s["legs"],
                "full_share": full_share,
                "instant_share": instant_share,
                "median_ride": median_ride,
                "verdict": verdict,
                "currently_excluded": driver.exclude_from_timing,
            })

        rows.sort(key=lambda r: -r["instant_share"])

        self.stdout.write(
            f"{'driver':<16}{'legs':>6}{'full%':>7}{'instant%':>10}"
            f"{'medRide':>9}  {'verdict':<9}{'flag now':<10}{'action'}"
        )
        self.stdout.write(
            "  full%    = share of legs with the complete on-the-way/picked-up/completed ladder\n"
            "  instant% = share of those where picked-up -> completed was under "
            f"{MIN_PICKUP_TO_COMPLETE} min (both buttons tapped at once)\n"
            "  medRide  = median ride EXCLUDING the instant ones. For an EXCLUDE driver this is\n"
            "             computed on the few trips they did tap separately, so a wild value here\n"
            "             (say 76m against a fleet norm of ~35m) means those taps were late too."
        )
        self.stdout.write("-" * 78)

        to_exclude, to_include = [], []
        for r in rows:
            action = ""
            if r["verdict"] == "EXCLUDE" and not r["currently_excluded"]:
                action = "-> EXCLUDE"
                to_exclude.append(r)
            elif r["verdict"] == "good" and r["currently_excluded"]:
                action = "-> re-include"
                to_include.append(r)

            line = (
                f"{r['name']:<16}{r['legs']:>6}{r['full_share']*100:>6.0f}%"
                f"{r['instant_share']*100:>9.0f}%{r['median_ride']:>8.0f}m  "
                f"{r['verdict']:<9}{('excluded' if r['currently_excluded'] else '-'):<10}{action}"
            )
            if r["verdict"] == "EXCLUDE":
                self.stdout.write(self.style.ERROR(line))
            elif action:
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(line)

        self._summarise(to_exclude, to_include)

        if not opts["apply"]:
            self.stdout.write(self.style.NOTICE(
                "\nReport only — nothing written. Re-run with --apply to set the flags.\n"
            ))
            return

        if not to_exclude and not to_include:
            self.stdout.write(self.style.SUCCESS("\nNothing to change.\n"))
            return

        if not opts["yes"]:
            answer = input("\nApply these flag changes? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                self.stdout.write(self.style.NOTICE("Aborted — nothing written."))
                return

        for r in to_exclude:
            r["driver"].exclude_from_timing = True
            r["driver"].save(update_fields=["exclude_from_timing"])
        for r in to_include:
            r["driver"].exclude_from_timing = False
            r["driver"].save(update_fields=["exclude_from_timing"])

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied: {len(to_exclude)} excluded, {len(to_include)} re-included.\n"
            "Re-run `update_all_route_timing_metrics` so the metrics reflect this.\n"
        ))

    def _summarise(self, to_exclude, to_include):
        if to_exclude:
            self.stdout.write(self.style.ERROR(
                f"\n{len(to_exclude)} driver(s) tapping 'Picked Up' and 'Complete' together "
                f"(>= {INSTANT_SHARE_EXCLUDE:.0%} of trips)."
            ))
            self.stdout.write(
                "  Their ride times are fiction. Recommend excluding from timing.\n"
                "  This is a coaching conversation, not an automated nudge."
            )
        if to_include:
            self.stdout.write(self.style.WARNING(
                f"\n{len(to_include)} driver(s) are currently excluded but produce GOOD data."
            ))
            self.stdout.write("  Recommend re-including them — you are discarding usable samples.")
        self.stdout.write(
            "\nNote: 'sparse' drivers are NOT excluded. A low full-chain rate means less\n"
            "data, not wrong data — the trips they do complete properly are still valid."
        )
