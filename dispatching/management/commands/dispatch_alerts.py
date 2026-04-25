"""
Management command to check for dispatch alerts and send ntfy notifications.

Run every 5-10 minutes via Windows Task Scheduler:
    python manage.py dispatch_alerts

Checks today's active legs for:
    1. Dispatch flags (not confirmed, not on the way, not picked up)
    2. Flight delays/mismatches (arrival time changed significantly)

Uses a JSON file to track already-notified alerts so the same issue
doesn't spam dispatchers repeatedly.
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from reservations.models import Leg
from reservations.utils import send_dispatch_alert_notification
from dispatching.utils import detect_leg_flags

logger = logging.getLogger(__name__)

# Alerts file lives next to manage.py — tracks (leg_id, flag_key) combos
ALERTS_FILE = Path(__file__).resolve().parents[3] / ".dispatch_alerts_sent.json"

# How long before we allow re-notifying the same alert (minutes)
RE_ALERT_COOLDOWN = 60


def _load_sent_alerts() -> dict:
    """Load previously sent alerts from JSON file. Returns {key: timestamp_iso}."""
    if ALERTS_FILE.exists():
        try:
            return json.loads(ALERTS_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_sent_alerts(alerts: dict):
    """Persist sent alerts to JSON file."""
    ALERTS_FILE.write_text(json.dumps(alerts, indent=2))


def _prune_old_alerts(alerts: dict) -> dict:
    """Remove entries older than 24 hours to keep the file small."""
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    return {k: v for k, v in alerts.items() if v > cutoff}


def _should_notify(sent_alerts: dict, key: str) -> bool:
    """Check if this alert key should fire (not sent recently)."""
    if key not in sent_alerts:
        return True
    last_sent = datetime.fromisoformat(sent_alerts[key])
    return (datetime.now() - last_sent).total_seconds() > RE_ALERT_COOLDOWN * 60


def _get_driver_first_name(driver):
    """Get driver's first name, fallback to username."""
    if driver.profile.first_name:
        return driver.profile.first_name
    return driver.profile.username


def _format_time(pickup_time):
    """Format a time object to '2:30 PM' style."""
    if not pickup_time:
        return "TBD"
    hour = pickup_time.hour
    minute = pickup_time.minute
    ampm = "AM" if hour < 12 else "PM"
    dh = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
    return f"{dh}:{minute:02d} {ampm}"


class Command(BaseCommand):
    help = "Check today's legs for dispatch issues and send ntfy alerts"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be sent without actually sending notifications",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        today = timezone.localdate()
        now = timezone.localtime().replace(tzinfo=None)

        # Fetch today's assigned, non-completed legs
        legs = (
            Leg.objects.filter(
                pickup_date=today,
                driver__isnull=False,
            )
            .exclude(status="completed")
            .select_related(
                "driver",
                "driver__profile",
                "reservation__customer",
                "reservation__vehicle", "vehicle",
                "flight_information",
            )
        )

        sent_alerts = _prune_old_alerts(_load_sent_alerts())
        new_alerts = 0

        for leg in legs:
            driver_first = _get_driver_first_name(leg.driver)
            customer_name = leg.reservation.customer.get_full_name()
            trip_type = leg.get_trip_type().title()
            time_str = _format_time(leg.pickup_time)
            pickup = leg.pickup_location or "Unknown"
            dropoff = leg.dropoff_location or "Unknown"

            # Calculate minutes until pickup for messaging
            if leg.pickup_time:
                pickup_dt = datetime.combine(leg.pickup_date, leg.pickup_time)
                mins_until = (pickup_dt - now).total_seconds() / 60
            else:
                mins_until = None

            # ── Dispatch flags ──
            flags = detect_leg_flags(leg, now)
            danger_flags = [f for f in flags if f["level"] == "danger"]

            for flag in danger_flags:
                key = f"flag:{leg.id}:{flag['text'][:30]}"
                if not _should_notify(sent_alerts, key):
                    continue

                # Aggressive, direct messaging
                if "not on the way" in flag["text"].lower():
                    if mins_until is not None and mins_until < 0:
                        mins_late = int(abs(mins_until))
                        title = f"NOT ON THE WAY - {driver_first} - {mins_late} min PAST pickup"
                        message = (
                            f"PICKUP WAS {mins_late} MIN AGO\n"
                            f"{driver_first} is not on the way for {customer_name}'s "
                            f"{time_str} {trip_type}\n\n"
                            f"{pickup} -> {dropoff}"
                        )
                    else:
                        mins_left = int(mins_until) if mins_until else 0
                        title = f"NOT ON THE WAY - {driver_first} - pickup in {mins_left} min"
                        message = (
                            f"PICKUP IN {mins_left} MIN\n"
                            f"{driver_first} is not on the way for {customer_name}'s "
                            f"{time_str} {trip_type}\n\n"
                            f"{pickup} -> {dropoff}"
                        )
                elif "not picked up" in flag["text"].lower():
                    mins_late = int(abs(mins_until)) if mins_until else 0
                    title = f"NOT PICKED UP - {driver_first} - {mins_late} min past pickup"
                    message = (
                        f"PICKUP WAS {mins_late} MIN AGO\n"
                        f"{driver_first} has not picked up {customer_name} "
                        f"for the {time_str} {trip_type}\n\n"
                        f"{pickup} -> {dropoff}"
                    )
                elif "not confirmed" in flag["text"].lower():
                    title = f"NOT CONFIRMED - {driver_first} - {time_str} {trip_type}"
                    message = (
                        f"{driver_first} has not confirmed {customer_name}'s "
                        f"{time_str} {trip_type}\n\n"
                        f"{pickup} -> {dropoff}"
                    )
                else:
                    title = f"ALERT - {driver_first} - {flag['text']}"
                    message = (
                        f"{driver_first}'s {time_str} {trip_type} for {customer_name}\n"
                        f"{pickup} -> {dropoff}\n\n"
                        f"{flag['text']}"
                    )

                if dry_run:
                    self.stdout.write(f"[DRY RUN] {title}\n  {message}\n")
                else:
                    send_dispatch_alert_notification(
                        title, message, priority="urgent",
                        tags=["rotating_light", "warning", "car"],
                    )
                    sent_alerts[key] = datetime.now().isoformat()
                    new_alerts += 1
                    logger.info(f"Dispatch alert sent: {title}")

            # ── Flight delay / mismatch ──
            mismatch = leg.get_flight_time_mismatch_display()
            if mismatch:
                key = f"flight:{leg.id}:{mismatch['direction']}:{mismatch['minutes']}"
                if _should_notify(sent_alerts, key):
                    flight = leg.flight_information
                    flight_label = ""
                    if flight:
                        airline = flight.airline_display_name or flight.airline or ""
                        flight_label = f"{airline} {flight.flight_number}".strip()

                    direction = mismatch["direction"].upper()
                    title = f"FLIGHT {direction} - {driver_first}'s {time_str} pickup - {customer_name}"
                    message = (
                        f"Flight {flight_label} is {mismatch['label']}\n"
                        f"{driver_first}'s {time_str} {trip_type} for {customer_name}\n\n"
                        f"{pickup} -> {dropoff}\n"
                        f"Pickup time may need adjusting"
                    )

                    if dry_run:
                        self.stdout.write(f"[DRY RUN] {title}\n  {message}\n")
                    else:
                        send_dispatch_alert_notification(
                            title, message, priority="high",
                            tags=["airplane", "warning", "clock"],
                        )
                        sent_alerts[key] = datetime.now().isoformat()
                        new_alerts += 1
                        logger.info(f"Flight alert sent: {title}")

        if not dry_run:
            _save_sent_alerts(sent_alerts)

        self.stdout.write(
            self.style.SUCCESS(
                f"Checked {legs.count()} legs — sent {new_alerts} alert(s)"
            )
        )
