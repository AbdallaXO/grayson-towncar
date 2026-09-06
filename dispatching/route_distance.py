"""
Persistent, precomputed drive-time cache for unknown/far ADDRESS pairs.

Why this exists
---------------
dispatching/scheduler.py estimates a leg's drive time from a coarse *category*
table (DRIVE_TIME_ESTIMATES). That table only knows Orlando landmarks; any stop
it can't place — a residential address, an odd hotel, a Tampa pickup — falls into
a bucket and gets the flat ~35-min guess. A house in Umatilla (a real ~55-60 min
drive to MCO) is billed the same as a house 10 min from the airport.

The obvious fix — call Google Distance Matrix live in resolve_drive_minutes — is
what caused the 2026-05-31 capacity-planner WORKER TIMEOUT (a synchronous 5s HTTP
call in the per-request render path, run for every leg on every page load) and the
2026-06-10 $593 cost spike (a harness fanning it out over thousands of legs). So
USE_LIVE_DISTANCE is default-OFF.

This module is the fix the scheduler.py comment asks for: a precomputed,
offline-cached matrix with NO in-request network.

  * Read path  — `cached_drive_minutes()` does ONE indexed DB read and returns the
                 stored minutes, or None (never a network call). On a miss it
                 records a `pending` row so the resolver knows to fill it.
  * Fill path  — `resolve_pending()` calls Google for pending/stale rows in bounded
                 batches. Runs OFF the request path: the `resolve_route_distances`
                 management command (Railway cron / schedulers process), and an
                 optional tightly-bounded fire-and-forget thread for web-only setups.

Each distinct address pair is paid for once and refreshed occasionally, so cost is
bounded by the number of unique unknown-route pairs, not by page views.
"""

import hashlib
import logging
import threading
import time as _time

from django.conf import settings
from django.core.cache import caches
from django.utils import timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (overridable via settings; sane defaults baked in)
# ---------------------------------------------------------------------------
HORIZON_DAYS = getattr(settings, "ROUTE_DISTANCE_HORIZON_DAYS", 21)   # how far ahead to precompute
REFRESH_DAYS = getattr(settings, "ROUTE_DISTANCE_REFRESH_DAYS", 30)   # re-pull a resolved pair after N days
MAX_ATTEMPTS = getattr(settings, "ROUTE_DISTANCE_MAX_ATTEMPTS", 4)    # give up on a bad address after N tries
# Opt-in booster for web-only deploys with no cron/worker: on a cache miss the
# render path kicks a detached, bounded background thread to fill pending rows.
# The response itself never waits on it. Default ON; set False to rely purely on
# the `resolve_route_distances` command.
INLINE_RESOLVER = getattr(settings, "ROUTE_DISTANCE_INLINE_RESOLVER", True)
INLINE_BATCH = getattr(settings, "ROUTE_DISTANCE_INLINE_BATCH", 8)
INLINE_THROTTLE_SECONDS = getattr(settings, "ROUTE_DISTANCE_INLINE_THROTTLE", 45)

# Short-lived process cache in front of the DB read, so the same pair rendered
# many times on one board doesn't re-hit the DB. Durable truth is the DB table.
_cache = caches["default"]
_PROCESS_CACHE_TTL = 60 * 30  # 30 min
_PENDING_SENTINEL = "__pending__"

# Inline-resolver throttle state (per web process).
_inline_lock = threading.Lock()
_inline_last_run = [0.0]

# ---------------------------------------------------------------------------
# Probe mode (Build 3b, Ticket D's billing wall)
# ---------------------------------------------------------------------------
# The Day-Builder scores HYPOTHETICAL boards through the shipped pipeline —
# dozens of adjacencies the real board may never have. Inside this window a
# cache lookup behaves exactly as normal when a value is KNOWN, but a miss is
# strictly read-only: no pending row is enqueued, no resolver is kicked, no
# process-cache sentinel is written (a probe must never suppress a later real
# enqueue either). The caller falls back to the category estimate, exactly as
# it already does for any unknown pair. Default behaviour with the flag off is
# byte-identical — this can only ever REDUCE what reaches the billed resolver.
_probe_tl = threading.local()


class probe_mode:
    """``with route_distance.probe_mode(): ...`` — read-only cache window."""

    def __enter__(self):
        self._prev = getattr(_probe_tl, "on", False)
        _probe_tl.on = True
        return self

    def __exit__(self, *exc):
        _probe_tl.on = self._prev
        return False


def _probing():
    return getattr(_probe_tl, "on", False)


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def pair_hash(pickup_text: str, dropoff_text: str) -> str:
    raw = f"{_normalize(pickup_text)}||{_normalize(dropoff_text)}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def cached_drive_minutes(pickup_text: str, dropoff_text: str):
    """
    Return the precomputed drive time (minutes) for this address pair, or None.

    NEVER makes a network call. On a miss it records a `pending` row (so the
    background resolver picks it up) and, if enabled, kicks the inline resolver.
    Any failure degrades silently to None so the render path always falls back to
    the category estimate — this must never break a page load.
    """
    if not pickup_text or not dropoff_text:
        return None
    try:
        h = pair_hash(pickup_text, dropoff_text)

        proc = _cache.get(_ckey(h))
        if proc is not None:
            return None if proc == _PENDING_SENTINEL else proc

        from reservations.models import RouteDistanceCache
        row = (
            RouteDistanceCache.objects
            .filter(pair_hash=h)
            .values("status", "drive_minutes")
            .first()
        )
        if row is None:
            if _probing():          # planning probe: read-only, never enqueue
                return None
            _enqueue(h, pickup_text, dropoff_text)
            _cache.set(_ckey(h), _PENDING_SENTINEL, _PROCESS_CACHE_TTL)
            _maybe_kick_inline_resolver()
            return None
        if row["status"] == "ok" and row["drive_minutes"] is not None:
            _cache.set(_ckey(h), row["drive_minutes"], _PROCESS_CACHE_TTL)
            return row["drive_minutes"]
        # pending or failed → no usable value yet
        _cache.set(_ckey(h), _PENDING_SENTINEL, _PROCESS_CACHE_TTL)
        if row["status"] == "pending" and not _probing():
            _maybe_kick_inline_resolver()
        return None
    except Exception:
        # Table missing (pre-migration), DB hiccup, anything — fall back quietly.
        logger.debug("route_distance cache lookup failed", exc_info=True)
        return None


def _ckey(h: str) -> str:
    return f"rdc_{h}"


def _enqueue(h: str, pickup_text: str, dropoff_text: str):
    from reservations.models import RouteDistanceCache
    RouteDistanceCache.objects.get_or_create(
        pair_hash=h,
        defaults={
            "pickup_text": pickup_text[:500],
            "dropoff_text": dropoff_text[:500],
            "status": RouteDistanceCache.STATUS_PENDING,
        },
    )


def enqueue_pair(pickup_text: str, dropoff_text: str) -> bool:
    """Public helper: ensure a pending row exists for this pair. Returns True if
    it is a resolvable pair (both ends present, not identical)."""
    if not pickup_text or not dropoff_text:
        return False
    if _normalize(pickup_text) == _normalize(dropoff_text):
        return False
    try:
        _enqueue(pair_hash(pickup_text, dropoff_text), pickup_text, dropoff_text)
        return True
    except Exception:
        logger.debug("route_distance enqueue failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Fill path (calls Google — runs OFF the request/response cycle)
# ---------------------------------------------------------------------------
def resolve_pending(batch=25, refresh_days=REFRESH_DAYS, max_attempts=MAX_ATTEMPTS):
    """
    Resolve up to `batch` pending/stale rows via Google Distance Matrix.

    Returns (resolved, failed, skipped). Safe to run concurrently: each row is
    claimed with SELECT ... FOR UPDATE SKIP LOCKED so two resolvers never pay for
    the same pair. This is the ONLY place drivers.utils.get_drive_time (the paid
    Distance Matrix call) is invoked from the scheduling side.
    """
    from django.db import transaction
    from django.db.models import Q
    from reservations.models import RouteDistanceCache
    from drivers.utils import get_drive_time as maps_drive_time

    stale_before = timezone.now() - timezone.timedelta(days=refresh_days)
    work = (
        Q(status=RouteDistanceCache.STATUS_PENDING)
        | Q(status=RouteDistanceCache.STATUS_FAILED, attempts__lt=max_attempts)
        | Q(status=RouteDistanceCache.STATUS_OK, resolved_at__lt=stale_before)
    )

    resolved = failed = skipped = 0
    seen_ids = []  # rows touched this run — so one bad/transient row can't be
                   # re-picked every iteration and starve the rest of the batch
                   # (and so `attempts` increments once per RUN, spacing retries).
    for _ in range(batch):
        with transaction.atomic():
            row = (
                RouteDistanceCache.objects
                .select_for_update(skip_locked=True)
                .filter(work)
                .exclude(id__in=seen_ids)
                .order_by("status", "created_at")  # pending (< 'ok') first
                .first()
            )
            if row is None:
                break
            seen_ids.append(row.id)
            # Mark claimed inside the lock so a sibling resolver won't re-grab it.
            row.attempts = (row.attempts or 0) + 1
            row.save(update_fields=["attempts", "updated_at"])

        info = None
        try:
            info = maps_drive_time(row.pickup_text, row.dropoff_text)
        except Exception as exc:  # network / API error
            row.last_error = str(exc)[:255]

        if info and info.get("duration_seconds"):
            row.drive_minutes = max(1, round(info["duration_seconds"] / 60))
            row.distance_text = (info.get("distance_text") or "")[:50]
            row.status = RouteDistanceCache.STATUS_OK
            row.last_error = ""
            row.resolved_at = timezone.now()
            row.save(update_fields=[
                "drive_minutes", "distance_text", "status",
                "last_error", "resolved_at", "updated_at",
            ])
            _cache.set(_ckey(row.pair_hash), row.drive_minutes, _PROCESS_CACHE_TTL)
            resolved += 1
        else:
            # No result: missing API key locally, unresolvable address, or API miss.
            if row.attempts >= max_attempts:
                row.status = RouteDistanceCache.STATUS_FAILED
                if not row.last_error:
                    row.last_error = "no route / no result from Distance Matrix"
                row.save(update_fields=["status", "last_error", "updated_at"])
                failed += 1
            else:
                # leave as-is (pending/failed) for a later retry
                row.save(update_fields=["last_error", "updated_at"])
                skipped += 1

    return resolved, failed, skipped


def enqueue_upcoming_legs(horizon_days=HORIZON_DAYS):
    """
    Proactively create pending rows for upcoming legs whose route the category
    table can't place — so distances are precomputed before anyone opens the board,
    not just for legs someone happened to view. Returns the number enqueued.
    """
    from datetime import timedelta
    from reservations.models import Leg
    from .analytics import categorize_location
    from .scheduler import LIVE_DISTANCE_UNKNOWN_CATS, INTRA_CLUSTER_LIVE_CATS

    today = timezone.localdate()
    legs = (
        Leg.objects
        .filter(pickup_date__gte=today, pickup_date__lte=today + timedelta(days=horizon_days))
        .exclude(status__in=["completed", "cancelled"])
        .values("pickup_location", "dropoff_location")
    )

    enqueued = 0
    seen = set()
    for leg in legs.iterator():
        pu = leg["pickup_location"]
        do = leg["dropoff_location"]
        if not pu or not do or _normalize(pu) == _normalize(do):
            continue
        h = pair_hash(pu, do)
        if h in seen:
            continue
        seen.add(h)
        pu_cat = categorize_location(pu)
        do_cat = categorize_location(do)
        unknown = (
            pu_cat in LIVE_DISTANCE_UNKNOWN_CATS
            or do_cat in LIVE_DISTANCE_UNKNOWN_CATS
            or (pu_cat == do_cat and pu_cat in INTRA_CLUSTER_LIVE_CATS)
        )
        if not unknown:
            continue
        _, created = _get_or_create_returning(h, pu, do)
        if created:
            enqueued += 1
    return enqueued


def _get_or_create_returning(h, pu, do):
    from reservations.models import RouteDistanceCache
    return RouteDistanceCache.objects.get_or_create(
        pair_hash=h,
        defaults={
            "pickup_text": pu[:500],
            "dropoff_text": do[:500],
            "status": RouteDistanceCache.STATUS_PENDING,
        },
    )


# ---------------------------------------------------------------------------
# Inline resolver (web-only fallback; detached, bounded, throttled)
# ---------------------------------------------------------------------------
def _maybe_kick_inline_resolver():
    """
    Fire-and-forget a tiny background fill so the feature works on a web-only
    deploy with no cron/worker. The current request NEVER waits on this.

    Safety rails (the 2026-07-18 outage was always-on scheduler threads × workers):
      * at most ONE resolver thread per process (non-blocking lock),
      * throttled to once per INLINE_THROTTLE_SECONDS,
      * resolves a small INLINE_BATCH then exits and closes its DB connection.
    """
    if not INLINE_RESOLVER:
        return
    if not _inline_lock.acquire(blocking=False):
        return
    now = _time.monotonic()
    if now - _inline_last_run[0] < INLINE_THROTTLE_SECONDS:
        _inline_lock.release()
        return
    _inline_last_run[0] = now

    def _run():
        try:
            resolve_pending(batch=INLINE_BATCH)
        except Exception:
            logger.debug("inline route-distance resolver failed", exc_info=True)
        finally:
            try:
                from django.db import connection
                connection.close()
            finally:
                _inline_lock.release()

    threading.Thread(target=_run, name="route-distance-inline", daemon=True).start()
