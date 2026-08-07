"""
Cloudflare Turnstile verification for public forms.

Turnstile is the layer that stops a bot before it has said anything: the
browser has to solve a challenge to get a token, and without a valid token the
submission never becomes a row, an email, or a dispatcher task. The content
scoring in users/spam.py is what catches whatever still gets through.

Configuration:

  * ``TURNSTILE_SITE_KEY``  — public, defaults to the widget created in the
    Cloudflare dashboard. Safe to ship in page source.
  * ``TURNSTILE_SECRET``    — set in the environment on the backend only.
    Never committed, never logged, never sent to the browser.

Until the secret is present this module is a no-op: no widget renders and every
submission is allowed through, so local dev and CI behave exactly as before.

Failure policy: FAIL CLOSED. If siteverify returns a non-2xx, a non-JSON body,
or the request errors out, the submission is rejected. This follows Cloudflare's
canonical integration. The tradeoff is deliberate and worth knowing: during a
siteverify outage, legitimate customers cannot submit the contact form and are
pushed to the phone number shown in the error message. Flip
``FAIL_OPEN_ON_UNREACHABLE`` to True to prefer taking a little spam over turning
customers away.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Cloudflare answers in well under a second; a customer should never sit and
# wait on this.
VERIFY_TIMEOUT_SECONDS = 5

# The field the Turnstile widget injects into the form.
TOKEN_FIELD = "cf-turnstile-response"

# Canonical Cloudflare behaviour is to reject when siteverify cannot be reached.
# See the module docstring for what changing this costs and buys.
FAIL_OPEN_ON_UNREACHABLE = False


def is_configured():
    """True once the secret is present. Everything is a no-op until then."""
    return bool(
        getattr(settings, "TURNSTILE_SECRET", "")
        and getattr(settings, "TURNSTILE_SITE_KEY", "")
    )


def site_key():
    """Public key for the widget, or empty string when we shouldn't render it."""
    if not is_configured():
        return ""
    return getattr(settings, "TURNSTILE_SITE_KEY", "")


def verify(token, remote_ip=None):
    """
    Check a Turnstile token against Cloudflare siteverify.

    Returns ``(passed, reason)``. ``reason`` is a short slug for logging —
    "not_configured", "missing_token", "invalid_token", "unreachable", "ok".
    """
    if not is_configured():
        return True, "not_configured"

    if not token:
        # A real browser that rendered the widget always posts a token. A
        # missing one means the form was submitted by something that never
        # loaded the page.
        return False, "missing_token"

    payload = {
        "secret": settings.TURNSTILE_SECRET,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        response = requests.post(
            VERIFY_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise requests.HTTPError(f"siteverify {response.status_code}")
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        # Network error, non-2xx, or non-JSON body. Fail closed per Cloudflare's
        # canonical integration. Logged at warning so a sustained outage is
        # visible rather than quietly turning customers away.
        logger.warning("Turnstile verification failed to complete: %s", exc)
        return FAIL_OPEN_ON_UNREACHABLE, "unreachable"

    if result.get("success") is True:
        return True, "ok"

    logger.info(
        "Turnstile rejected a submission from %s: %s",
        remote_ip or "unknown",
        result.get("error-codes", []),
    )
    return False, "invalid_token"


def verify_request(request, client_ip=None):
    """
    Convenience wrapper: pull the token straight off a Django POST.

    Returns ``(passed, reason)``.
    """
    return verify(request.POST.get(TOKEN_FIELD, ""), remote_ip=client_ip)
