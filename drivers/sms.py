"""Shared Twilio SMS sending, reused by every driver-facing notification
(time-off requests, document uploads, wake-up escalations). Silently no-ops
when Twilio isn't configured, so a notification failure never blocks the
flow that triggered it.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def normalize_e164(number):
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


def client():
    """(Client, from_number) for the shared Twilio account, or (None, None)
    when it isn't configured. Public — wakeup.py's voice calls need the raw
    client too, not just send()'s SMS-only wrapper."""
    sid = (getattr(settings, "TWILIO_ACCOUNT_SID", None) or "").strip()
    auth = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    from_number = normalize_e164(getattr(settings, "TWILIO_PHONE_NUMBER", None))
    if not sid or not auth or not from_number:
        return None, None
    try:
        from twilio.rest import Client
        return Client(sid, auth), from_number
    except ImportError:
        return None, None


def send(to_number, body):
    to = normalize_e164(to_number)
    if not to:
        return False, "missing recipient"
    twilio_client, from_number = client()
    if twilio_client is None:
        logger.info("Twilio not configured; skipping SMS to %s", to)
        return False, "twilio not configured"
    try:
        twilio_client.messages.create(body=body, from_=from_number, to=to)
        return True, None
    except Exception as e:
        logger.exception("Failed to send SMS to %s", to)
        return False, str(e)
