"""
Flight verification emails — sent to a guest when AeroAPI can't find their
booked flight (typically wrong/old flight number, or guest entered the
first leg of a connection instead of the final Orlando-bound leg).

Surface:
- `send_flight_verification_email(leg, sent_by, request=None)` — render + send
- `make_verify_token(leg_id)` / `parse_verify_token(token)` — signed,
  expiring tokens for the public self-service update link.
"""

import logging
import threading
import time

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Tokens stay valid for two weeks. Long enough for a guest who opens the email
# the next day to still click through, short enough that an old link won't
# overwrite a flight someone fixed manually weeks later.
VERIFY_TOKEN_MAX_AGE_SECONDS = 14 * 24 * 3600

_SIGNER_SALT = "dispatching.flight_verify_email"
_REPLY_TO = "reservations@graysontowncar.com"
_FROM_EMAIL = "reservations@graysontowncar.com"


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_SIGNER_SALT)


def make_verify_token(leg_id: int) -> str:
    """Generate a signed, expiring token that encodes a leg id."""
    return _signer().sign(str(int(leg_id)))


def parse_verify_token(token: str) -> int:
    """
    Return the leg_id encoded in the token, or raise BadSignature /
    SignatureExpired. Caller is responsible for catching.
    """
    raw = _signer().unsign(token, max_age=VERIFY_TOKEN_MAX_AGE_SECONDS)
    return int(raw)


def _build_verify_url(token: str, request=None) -> str:
    """
    Build the absolute URL for the self-service page. Uses request.build_absolute_uri
    when available; otherwise falls back to SITE_BASE_URL setting.
    """
    path = reverse("flight_verification_public", args=[token])
    if request is not None:
        try:
            return request.build_absolute_uri(path)
        except Exception:
            pass
    base = (
        getattr(settings, "SITE_BASE_URL", None)
        or getattr(settings, "BASE_URL", None)
        or "https://graysontowncar.com"
    )
    return f"{base.rstrip('/')}{path}"


def _send_in_background(send_callable, max_retries: int = 3):
    """Fire-and-forget retrying send. Mirrors users.emails._send_email_with_retry."""

    def runner():
        for attempt in range(max_retries):
            try:
                send_callable()
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        f"flight_verify email attempt {attempt + 1} failed, "
                        f"retrying in {wait}s: {e}"
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        f"flight_verify email failed after {max_retries} attempts: {e}"
                    )

    t = threading.Thread(target=runner, daemon=True)
    t.start()


def send_flight_verification_email(leg, sent_by=None, request=None) -> dict:
    """
    Send a flight-verification email to the leg's guest. Returns a dict
    summarizing what happened — caller can pass it straight back to the UI.

    Background-sends so the staff UI doesn't block on SMTP.
    """
    reservation = getattr(leg, "reservation", None)
    customer = getattr(reservation, "customer", None) if reservation else None
    to_email = (getattr(customer, "email", "") or "").strip()
    if not to_email:
        return {"success": False, "error": "Guest has no email on file."}

    flight = getattr(leg, "flight_information", None)
    if not flight:
        return {
            "success": False,
            "error": "This leg has no flight record to verify.",
        }

    try:
        trip_type = leg.get_trip_type()
    except Exception:
        trip_type = "other"

    token = make_verify_token(leg.id)
    verify_url = _build_verify_url(token, request=request)

    airline_display = (
        getattr(flight, "airline_display_name", "")
        or getattr(flight, "airline", "")
        or ""
    )
    flight_number = getattr(flight, "flight_number", "") or ""
    booked_label = f"{airline_display} {flight_number}".strip() or "your booked flight"

    first_name = (getattr(customer, "first_name", "") or "").strip().title() or "there"

    # Friendly "in X days" / "tomorrow" / "today" phrase for the opening line.
    # If pickup is in the past or unknown, fall back to a neutral phrasing so we
    # never emit "in -2 days" type weirdness.
    pickup_when_phrase = "coming up soon"
    if leg.pickup_date:
        today = timezone.localdate()
        days_until = (leg.pickup_date - today).days
        if days_until <= 0:
            pickup_when_phrase = "coming up very soon"
        elif days_until == 1:
            pickup_when_phrase = "tomorrow"
        else:
            pickup_when_phrase = f"in {days_until} days"

    context = {
        "customer_first_name": first_name,
        "reservation": reservation,
        "leg": leg,
        "flight": flight,
        "booked_label": booked_label,
        "airline_display": airline_display,
        "flight_number": flight_number,
        "trip_type": trip_type,
        "is_arrival": trip_type == "arrival",
        "is_return": trip_type == "return",
        "verify_url": verify_url,
        "reply_to": _REPLY_TO,
        "pickup_location": leg.pickup_location or "",
        "dropoff_location": leg.dropoff_location or "",
        "pickup_date": leg.pickup_date,
        "pickup_time": leg.pickup_time,
        "pickup_when_phrase": pickup_when_phrase,
    }

    if leg.pickup_date:
        subject = f"Please confirm your flight for our pickup on {leg.pickup_date.strftime('%a, %b %-d')}"
    else:
        subject = "Please confirm your flight for our pickup"

    html_content = render_to_string("users/flight_verification_email.html", context)

    def _do_send():
        msg = EmailMultiAlternatives(
            subject=subject,
            body="",
            from_email=_FROM_EMAIL,
            to=[to_email],
            reply_to=[_REPLY_TO],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        logger.info(
            f"flight_verify email sent for leg={leg.id} reservation={getattr(reservation, 'id', None)} to={to_email}"
        )
        try:
            from users.emails import log_email_sent
            log_email_sent(
                email_type="flight_verification",
                recipient_email=to_email,
                subject=subject,
                sent_by=sent_by,
                reservation=reservation,
                metadata={"leg_id": leg.id, "verify_url": verify_url},
            )
        except Exception as log_err:
            logger.warning(f"flight_verify email-log write failed: {log_err}")

    _send_in_background(_do_send)

    sent_at = timezone.now()

    # Persist the timestamp on the leg so the UI can show "Email sent X ago"
    # instead of re-offering the verify button. Cleared when the guest acts
    # via the public form (flight_verification_public).
    try:
        from reservations.models import Leg as _Leg
        _Leg.objects.filter(pk=leg.pk).update(
            flight_verification_email_sent_at=sent_at
        )
        leg.flight_verification_email_sent_at = sent_at
    except Exception as stamp_err:
        logger.warning(
            f"flight_verify: failed to stamp sent_at on leg {leg.id}: {stamp_err}"
        )

    # Log a CommunicationAttempt on the open FLIGHT_VERIFICATION task (if any)
    # so the task's comm history shows the email went out. Mirrors the pattern
    # used by users.emails.send_payment_reminder.
    try:
        from ops.models import OperationalTask, CommunicationAttempt
        from ops.services import log_communication

        open_task = (
            OperationalTask.objects.filter(
                leg=leg,
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                status__in=list(OperationalTask.OPEN_STATUSES),
            )
            .order_by("-created_at")
            .first()
        )
        if open_task is not None:
            comm_meta = {"verify_url": verify_url, "leg_id": leg.id}
            if sent_by is not None:
                log_communication(
                    open_task,
                    channel=CommunicationAttempt.Channel.EMAIL,
                    outcome=CommunicationAttempt.Outcome.SENT,
                    user=sent_by,
                    notes="Flight verification email sent to guest",
                    contact_value=to_email,
                    metadata=comm_meta,
                )
            else:
                CommunicationAttempt.objects.create(
                    task=open_task,
                    channel=CommunicationAttempt.Channel.EMAIL,
                    outcome=CommunicationAttempt.Outcome.SENT,
                    staff_user=None,
                    notes="Flight verification email sent to guest (automated)",
                    contact_value=to_email,
                    metadata=comm_meta,
                )
                OperationalTask.objects.filter(pk=open_task.pk).update(
                    attempts=open_task.attempts + 1, last_attempt_at=sent_at
                )
    except Exception as comm_err:
        logger.warning(
            f"flight_verify: comm log failed for leg {leg.id}: {comm_err}"
        )

    return {
        "success": True,
        "recipient": to_email,
        "verify_url": verify_url,
        "sent_at": sent_at.isoformat(),
    }


# Internal address that receives "guest just updated their flight" heads-ups.
# Same inbox as send_internal_confirmation in users.emails — keeps these in
# the dispatcher's existing workflow rather than spinning up a new alias.
_OFFICE_NOTIFY_EMAIL = "reservations@graysontowncar.com"


def send_flight_updated_notifications(
    *,
    leg,
    old_flight_label: str,
    new_flight_label: str,
    pickup_adjusted: bool,
    old_pickup_time_str: str = "",
    new_pickup_time_str: str = "",
    flight_arrives_different_date: bool = False,
    request=None,
) -> dict:
    """
    After a guest submits the self-service verification form, send two notes:

    1. **Guest confirmation** — "Here's what we have now" so they have a record
       and can spot any further mistakes.
    2. **Office heads-up** — a short note in the dispatch inbox so a human knows
       the flight (and possibly pickup time) just changed, especially useful
       when the change happens minutes before pickup or the flight slipped to
       a different day.

    Both sends are best-effort (background thread, retries) — failures here are
    logged but do not surface to the guest.
    """
    reservation = getattr(leg, "reservation", None)
    customer = getattr(reservation, "customer", None) if reservation else None
    flight = getattr(leg, "flight_information", None)

    first_name = (getattr(customer, "first_name", "") or "").strip().title() or "there"
    full_name = (
        f"{(getattr(customer, 'first_name', '') or '').strip()} "
        f"{(getattr(customer, 'last_name', '') or '').strip()}"
    ).strip() or "Guest"
    guest_email = (getattr(customer, "email", "") or "").strip()

    # Build an absolute admin/dispatcher URL the dispatcher can click straight
    # to the reservation. Use the same logic as the verify link helper.
    reservation_url = ""
    try:
        if reservation is not None:
            path = reverse("reservation_details", args=[str(reservation.uuid)])
            if request is not None:
                reservation_url = request.build_absolute_uri(path)
            else:
                base = (
                    getattr(settings, "SITE_BASE_URL", None)
                    or getattr(settings, "BASE_URL", None)
                    or "https://graysontowncar.com"
                )
                reservation_url = f"{base.rstrip('/')}{path}"
    except Exception:
        reservation_url = ""

    context = {
        "customer_first_name": first_name,
        "customer_full_name": full_name,
        "reservation": reservation,
        "leg": leg,
        "flight": flight,
        "old_flight_label": old_flight_label,
        "new_flight_label": new_flight_label,
        "pickup_adjusted": pickup_adjusted,
        "old_pickup_time_str": old_pickup_time_str,
        "new_pickup_time_str": new_pickup_time_str,
        "flight_arrives_different_date": flight_arrives_different_date,
        "pickup_date": leg.pickup_date,
        "pickup_time": leg.pickup_time,
        "pickup_location": leg.pickup_location or "",
        "dropoff_location": leg.dropoff_location or "",
        "reservation_url": reservation_url,
    }

    # ── Guest confirmation ──────────────────────────────────────────────
    guest_sent = False
    if guest_email:
        guest_subject = f"Flight confirmed — {new_flight_label}"
        try:
            guest_html = render_to_string(
                "users/flight_verification_updated_email.html", context
            )

            def _send_guest():
                msg = EmailMultiAlternatives(
                    subject=guest_subject,
                    body="",
                    from_email=_FROM_EMAIL,
                    to=[guest_email],
                    reply_to=[_REPLY_TO],
                )
                msg.attach_alternative(guest_html, "text/html")
                msg.send()
                logger.info(
                    f"flight_verify update-confirmation sent to guest leg={leg.id} to={guest_email}"
                )
                try:
                    from users.emails import log_email_sent
                    log_email_sent(
                        email_type="flight_verification_updated",
                        recipient_email=guest_email,
                        subject=guest_subject,
                        reservation=reservation,
                        metadata={
                            "leg_id": leg.id,
                            "old_flight": old_flight_label,
                            "new_flight": new_flight_label,
                            "pickup_adjusted": pickup_adjusted,
                        },
                    )
                except Exception as log_err:
                    logger.warning(f"flight_verify update-confirm log failed: {log_err}")

            _send_in_background(_send_guest)
            guest_sent = True
        except Exception as e:
            logger.warning(
                f"flight_verify: failed to queue guest confirmation for leg {leg.id}: {e}"
            )

    # ── Office heads-up ─────────────────────────────────────────────────
    pickup_date_str = leg.pickup_date.strftime("%a, %b %-d") if leg.pickup_date else "TBD"
    office_subject = (
        f"Flight updated by guest: {full_name} — {new_flight_label} ({pickup_date_str})"
    )
    try:
        office_html = render_to_string(
            "users/flight_verification_dispatch_notice.html", context
        )

        def _send_office():
            msg = EmailMultiAlternatives(
                subject=office_subject,
                body="",
                from_email=_FROM_EMAIL,
                to=[_OFFICE_NOTIFY_EMAIL],
                reply_to=[guest_email] if guest_email else None,
            )
            msg.attach_alternative(office_html, "text/html")
            msg.send()
            logger.info(
                f"flight_verify office heads-up sent for leg={leg.id} reservation={getattr(reservation, 'id', None)}"
            )
            try:
                from users.emails import log_email_sent
                log_email_sent(
                    email_type="flight_verification_dispatch_notice",
                    recipient_email=_OFFICE_NOTIFY_EMAIL,
                    subject=office_subject,
                    reservation=reservation,
                    metadata={
                        "leg_id": leg.id,
                        "old_flight": old_flight_label,
                        "new_flight": new_flight_label,
                        "pickup_adjusted": pickup_adjusted,
                        "flight_arrives_different_date": flight_arrives_different_date,
                    },
                )
            except Exception as log_err:
                logger.warning(f"flight_verify dispatch-notice log failed: {log_err}")

        _send_in_background(_send_office)
    except Exception as e:
        logger.warning(
            f"flight_verify: failed to queue office heads-up for leg {leg.id}: {e}"
        )

    return {"success": True, "guest_sent": guest_sent}
