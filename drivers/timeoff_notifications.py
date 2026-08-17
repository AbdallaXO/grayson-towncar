"""SMS notifications for the driver time-off request workflow.

Founders get a text on every new request. The submitting driver gets a text
when their request is approved or denied. Twilio is reused from the existing
confirmation pipeline; if it isn't configured, sends silently no-op so the
request flow never blocks on notification failure.
"""

import logging

from django.conf import settings
from business.datefmt import day_month, time12

logger = logging.getLogger(__name__)


def _normalize_e164(number):
    """Best-effort E.164 normalization for US numbers."""
    if not number:
        return ""
    digits = "".join(c for c in str(number) if c.isdigit() or c == "+")
    if digits.startswith("+"):
        return digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return digits


def _twilio_client():
    sid = (getattr(settings, "TWILIO_ACCOUNT_SID", None) or "").strip()
    auth = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    from_number = _normalize_e164(getattr(settings, "TWILIO_PHONE_NUMBER", None))
    if not sid or not auth or not from_number:
        return None, None
    try:
        from twilio.rest import Client
        return Client(sid, auth), from_number
    except ImportError:
        return None, None


def _send(to_number, body):
    to = _normalize_e164(to_number)
    if not to:
        return False, "missing recipient"
    client, from_number = _twilio_client()
    if client is None:
        logger.info("Twilio not configured; skipping time-off SMS to %s", to)
        return False, "twilio not configured"
    try:
        client.messages.create(body=body, from_=from_number, to=to)
        return True, None
    except Exception as e:
        logger.exception("Failed to send time-off SMS to %s", to)
        return False, str(e)


def _range_display(override):
    if override.end_date and override.end_date != override.date:
        return f"{day_month(override.date)} - {day_month(override.end_date)}"
    return day_month(override.date)


def _window_display(override):
    """Human description of the off window (full day vs partial)."""
    et = override.exception_type
    if et == "off":
        return "(all day)"
    if et == "available_until" and override.end_time:
        return f"(unavailable after {time12(override.end_time)})"
    if et == "available_after" and override.start_time:
        return f"(unavailable before {time12(override.start_time)})"
    if et == "unavailable_window" and override.start_time and override.end_time:
        return f"({time12(override.start_time)} - {time12(override.end_time)})"
    if et == "available_window" and override.start_time and override.end_time:
        return f"(only available {time12(override.start_time)} - {time12(override.end_time)})"
    return ""


def notify_founders_of_new_request(override):
    """Text every founder when a driver submits a request."""
    phones = getattr(settings, "TIMEOFF_NOTIFY_PHONES", []) or []
    if not phones:
        return
    driver_name = str(override.driver)
    when = _range_display(override)
    window = _window_display(override)
    reason = override.get_reason_display() if override.reason else ""
    note = f" - {override.notes}" if override.notes else ""

    body_lines = [
        f"NEW time-off request",
        f"{driver_name}: {when} {window}".strip(),
    ]
    if reason:
        body_lines.append(f"Reason: {reason}{note}")
    elif note:
        body_lines.append(note.lstrip(" -"))
    body_lines.append("Review in dispatcher portal.")
    body = "\n".join(body_lines)

    for phone in phones:
        _send(phone, body)


def notify_driver_of_decision(override):
    """Text the driver when their request is approved or denied."""
    driver_phone = (override.driver.phone_number or "").strip()
    if not driver_phone:
        return
    when = _range_display(override)
    window = _window_display(override)
    if override.status == "approved":
        body = f"Your time-off request for {when} {window} has been APPROVED. - Grayson Towncar".strip()
    elif override.status == "denied":
        body = f"Your time-off request for {when} {window} was DENIED.".strip()
        if override.denial_reason:
            body += f" Reason: {override.denial_reason}"
        body += " - Grayson Towncar"
    else:
        return
    _send(driver_phone, body)
