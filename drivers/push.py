"""Web Push for the driver portal.

Completely inert unless WEBPUSH_VAPID_PRIVATE_KEY / PUBLIC_KEY are set (same
gating pattern as Samsara). All sends happen off the request thread (daemon
threads / debounce timers) so the single gunicorn worker never blocks on the
browser push services — a send is just an HTTPS POST to Apple/Google/Mozilla.

Two layers:
- send_push_to_driver(): synchronous fan-out to every device a driver
  subscribed; prunes dead subscriptions (404/410).
- queue_schedule_notice(): per-driver debounce so one dispatch action that
  touches many legs (auto-assign apply, Day Setup) produces ONE notification
  instead of a buzz storm.
"""
import json
import logging
import threading

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 8

_pending_lock = threading.Lock()
_pending = {}  # driver_id -> {"events": [...], "timer": threading.Timer}


def push_enabled():
    return bool(
        settings.WEBPUSH_VAPID_PRIVATE_KEY and settings.WEBPUSH_VAPID_PUBLIC_KEY
    )


def send_push_to_driver(driver_id, title, body, url="/drivers/", tag="gt-driver"):
    """Send a notification to every device this driver subscribed.
    Synchronous — call from a background thread. Returns the delivered count."""
    if not push_enabled():
        return 0
    from pywebpush import webpush, WebPushException
    from drivers.models import DriverPushSubscription

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for sub in DriverPushSubscription.objects.filter(driver_id=driver_id):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
                # pywebpush mutates the claims dict (adds aud/exp) — fresh copy per send
                vapid_claims={"sub": f"mailto:{settings.WEBPUSH_VAPID_CLAIMS_EMAIL}"},
                ttl=12 * 3600,
            )
            sent += 1
            DriverPushSubscription.objects.filter(pk=sub.pk).update(
                last_success_at=timezone.now()
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                # Device unsubscribed or subscription expired — prune it
                sub.delete()
            else:
                logger.warning(
                    "Web push to driver %s failed (%s): %s", driver_id, status, exc
                )
        except Exception as exc:
            logger.warning("Web push to driver %s errored: %s", driver_id, exc)
    return sent


def queue_schedule_notice(driver_id, kind, leg):
    """Record a schedule change for a driver and (re)start their debounce
    timer. kind: 'new' | 'removed' | 'retimed' | 'cancelled'."""
    # Automatic notices are PAUSED until WEBPUSH_AUTO_NOTICES=true — the
    # subscribe/test plumbing stays live so devices can enroll ahead of time.
    if not settings.WEBPUSH_AUTO_NOTICES:
        return
    if not push_enabled() or not driver_id:
        return
    from drivers.models import DriverPushSubscription
    # No devices → no timer. Keeps the debounce machinery (and its daemon
    # threads) completely quiet for drivers who never turned the bell on.
    if not DriverPushSubscription.objects.filter(driver_id=driver_id).exists():
        return
    event = {
        "kind": kind,
        "date_iso": leg.pickup_date.isoformat() if leg.pickup_date else "",
        "date_label": (
            f"{leg.pickup_date.strftime('%a, %b')} {leg.pickup_date.day}"
            if leg.pickup_date else ""
        ),
        "time_label": (
            leg.pickup_time.strftime("%I:%M %p").lstrip("0")
            if leg.pickup_time else ""
        ),
        "pickup": (leg.pickup_location or "")[:60],
    }
    with _pending_lock:
        entry = _pending.setdefault(driver_id, {"events": [], "timer": None})
        entry["events"].append(event)
        if entry["timer"]:
            entry["timer"].cancel()
        timer = threading.Timer(DEBOUNCE_SECONDS, _flush_driver, args=(driver_id,))
        timer.daemon = True
        entry["timer"] = timer
        timer.start()


def _flush_driver(driver_id):
    with _pending_lock:
        entry = _pending.pop(driver_id, None)
    if not entry or not entry["events"]:
        return
    try:
        title, body, url = compose_notice(entry["events"])
        send_push_to_driver(driver_id, title, body, url=url, tag="gt-schedule")
    except Exception:
        logger.exception("Push flush failed for driver %s", driver_id)


_KIND_TITLE = {
    "new": "New trip assigned",
    "removed": "Trip removed from your schedule",
    "retimed": "Trip time changed",
    "cancelled": "Trip cancelled",
}


def compose_notice(events):
    """One event → a specific message; several → one coalesced summary."""
    if len(events) == 1:
        e = events[0]
        title = _KIND_TITLE.get(e["kind"], "Schedule updated")
        when = " · ".join(x for x in (e["date_label"], e["time_label"]) if x)
        body = " — ".join(x for x in (when, e["pickup"]) if x) or "Open the app for details."
        url = f"/drivers/?date={e['date_iso']}" if e["date_iso"] else "/drivers/"
        return title, body, url

    dated = sorted(
        {(e["date_iso"], e["date_label"]) for e in events if e["date_iso"]}
    )
    title = "Schedule updated"
    body = f"{len(events)} changes to your trips"
    if dated:
        body += f" starting {dated[0][1]}"
    url = f"/drivers/?date={dated[0][0]}" if dated else "/drivers/"
    return title, body, url
