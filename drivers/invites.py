"""Welcome links for new (or password-less) chauffeurs. See DriverInvite.

    invite = invites.create(driver, by=request.user)
    ok, err = invites.deliver(invite, "sms")          # or "email" / "link"
    ...
    invite = invites.lookup(token)                     # None unless open
    invites.accept(invite, password, username, email)  # sets the password

Nothing here is automated: every send is a person pressing a button on the
driver's profile, so it is deliberately NOT behind OUTBOUND_AUTOMATION_ENABLED.
"""
import logging
import secrets

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from drivers import phones, sms
from drivers.models import DriverInvite
from drivers.paperwork import first_name_for

logger = logging.getLogger(__name__)

COMPANY_PHONE = "407-212-7190"


def open_invite(driver):
    now = timezone.now()
    return (
        DriverInvite.objects.filter(
            driver=driver, accepted_at__isnull=True, revoked_at__isnull=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )


def latest_invite(driver):
    return DriverInvite.objects.filter(driver=driver).order_by("-created_at").first()


def revoke_open(driver, now=None):
    now = now or timezone.now()
    return DriverInvite.objects.filter(
        driver=driver, accepted_at__isnull=True, revoked_at__isnull=True,
    ).update(revoked_at=now)


def create(driver, by=None):
    """A fresh link. Any other open link for this driver stops working."""
    now = timezone.now()
    revoke_open(driver, now)
    return DriverInvite.objects.create(
        driver=driver,
        token=secrets.token_urlsafe(32),
        created_by=by if (by is not None and by.is_authenticated) else None,
        expires_at=now + DriverInvite.LIFETIME,
    )


def current_or_new(driver, by=None):
    """Reuse the open link if there is one, otherwise mint one."""
    return open_invite(driver) or create(driver, by=by)


def url(invite):
    return f"{settings.SITE_BASE_URL}{reverse('driver_welcome', args=[invite.token])}"


def lookup(token):
    """The open invite behind ``token``, or None (unknown, used, revoked, expired)."""
    if not token or len(token) > 64:
        return None
    try:
        invite = DriverInvite.objects.select_related("driver__profile").get(token=token)
    except DriverInvite.DoesNotExist:
        return None
    return invite if invite.is_open else None


def lookup_any(token):
    """Like lookup() but also returns a spent/expired invite, so the landing
    page can say *why* the link no longer works."""
    if not token or len(token) > 64:
        return None
    return DriverInvite.objects.select_related("driver__profile").filter(token=token).first()


# ── Delivery ─────────────────────────────────────────────────────────────────

def sms_body(invite):
    first = first_name_for(invite.driver)
    return (
        f"Hi {first}, welcome to Grayson Towncar. Tap this link to set up your "
        f"driver app login (it works for 7 days): {url(invite)}"
    )


def send_sms(invite):
    to = phones.normalize(invite.driver.phone_number)
    if not to:
        return False, "This driver has no valid mobile number on file."
    ok, err = sms.send(to, sms_body(invite))
    if ok:
        _mark_sent(invite, "sms", phones.pretty(to))
    return ok, (None if ok else _friendly_sms_error(err))


def send_email(invite):
    to = (invite.driver.profile.email or "").strip()
    if not to:
        return False, "This driver has no email address on file."
    context = {
        "first_name": first_name_for(invite.driver),
        "link": url(invite),
        "expires_at": invite.expires_at,
        "company_phone": COMPANY_PHONE,
    }
    subject = "Welcome to Grayson Towncar — set up your driver login"
    text = render_to_string("drivers/emails/driver_invite.txt", context)
    html = render_to_string("drivers/emails/driver_invite.html", context)
    try:
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [to])
        msg.attach_alternative(html, "text/html")
        msg.send()
    except Exception as e:  # pragma: no cover - transport failure
        logger.exception("Driver invite email to %s failed", to)
        return False, f"The email could not be sent ({e})."
    _mark_sent(invite, "email", to)
    return True, None


def deliver(invite, via):
    """Send (or hand over) the link. ``via`` is sms / email / link."""
    if via == "sms":
        return send_sms(invite)
    if via == "email":
        return send_email(invite)
    _mark_sent(invite, "link", "")
    return True, None


def _mark_sent(invite, via, to):
    invite.sent_via = via
    invite.sent_to = to
    invite.last_sent_at = timezone.now()
    invite.send_count = (invite.send_count or 0) + 1
    invite.save(update_fields=["sent_via", "sent_to", "last_sent_at", "send_count"])


def _friendly_sms_error(err):
    if err == "twilio not configured":
        return "Texting isn't set up on this server — copy the link and send it yourself."
    return f"The text could not be sent ({err})."


# ── Acceptance ───────────────────────────────────────────────────────────────

def accept(invite, password, username=None, email=None):
    """Set the driver's password (and optionally their username and email),
    mark the invite used, and return the User ready to be logged in."""
    user = invite.driver.profile
    if username and username != user.username:
        user.username = username
    if email:
        user.email = email
    user.set_password(password)
    user.is_active = True
    user.save()
    now = timezone.now()
    DriverInvite.objects.filter(pk=invite.pk).update(accepted_at=now)
    invite.accepted_at = now
    # Any other open link for this person is now pointless.
    DriverInvite.objects.filter(
        driver=invite.driver, accepted_at__isnull=True, revoked_at__isnull=True,
    ).exclude(pk=invite.pk).update(revoked_at=now)
    return user


# ── Username generation ──────────────────────────────────────────────────────

def suggest_username(first_name, last_name, phone=""):
    """``first.last`` in lowercase ASCII, de-duplicated with a numeric suffix.
    Falls back to the mobile's last four digits when the name is unusable."""
    from django.contrib.auth.models import User
    import re
    import unicodedata

    def slug(part):
        part = unicodedata.normalize("NFKD", part or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", part.lower())

    first, last = slug(first_name), slug(last_name)
    base = ".".join(p for p in (first, last) if p)
    if not base:
        digits = phones.digits_only(phone)
        base = f"driver{digits[-4:]}" if digits else "driver"
    candidate, n = base, 2
    while User.objects.filter(username__iexact=candidate).exists():
        candidate = f"{base}{n}"
        n += 1
    return candidate
