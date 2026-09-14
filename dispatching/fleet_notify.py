"""
The two texts the fleet job gets, and the rule that keeps it to two.

  * A reported vehicle issue — a dispatcher (or a chauffeur through them)
    says something is wrong with a car. Human-originated, actionable, rare.
    Sent the moment it's filed.
  * The morning digest — once a day, only when the NOW group of the
    attention list is not empty, listing the top few lines. Quiet days send
    nothing at all.

Nothing else texts. A fault code appearing is NOT a text (transient codes
clear within a poll or two and would train everyone to ignore the number);
it is a line on the desk and, if still there in the morning, in the digest.
This is the same instinct as the no-automation rule for chauffeurs
(docs/release-notes/README.md): the system informs, a person decides.

Recipients are the profiles flagged ``is_fleet_manager`` (with a phone
number) plus ``FLEET_NOTIFY_PHONES`` from the environment. The master switch
``FLEET_ALERTS_ENABLED`` defaults OFF for the same reason the wake-up calls
do: a developer's copy of the database carries real phone numbers.
"""
from __future__ import annotations

import logging

from django.conf import settings

from business.datefmt import strf
from drivers import sms

logger = logging.getLogger(__name__)

DIGEST_MAX_LINES = 4


def alerts_enabled() -> bool:
    if getattr(settings, "TESTING", False):
        return False
    return bool(getattr(settings, "FLEET_ALERTS_ENABLED", False))


def recipients():
    """Phone numbers, de-duplicated, in a stable order."""
    from users.models import UserProfile

    phones = []
    for profile in (UserProfile.objects.filter(is_fleet_manager=True, user__is_active=True)
                    .exclude(phone_number="").order_by("user__username")):
        phones.append(sms.normalize_e164(profile.phone_number))
    for phone in getattr(settings, "FLEET_NOTIFY_PHONES", []) or []:
        phones.append(sms.normalize_e164(phone))
    seen, out = set(), []
    for p in phones:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _send_all(body):
    if not alerts_enabled():
        logger.info("Fleet alerts disabled; not sending: %s", body[:80])
        return {"sent": 0, "skipped": "disabled"}
    phones = recipients()
    if not phones:
        logger.info("No fleet alert recipients; not sending: %s", body[:80])
        return {"sent": 0, "skipped": "no_recipients"}
    sent = 0
    for phone in phones:
        ok, _err = sms.send(phone, body)
        sent += 1 if ok else 0
    return {"sent": sent, "skipped": ""}


def issue_text(issue) -> str:
    who = ""
    if issue.reported_by:
        who = issue.reported_by.get_full_name() or issue.reported_by.username
    who = who or issue.get_source_display()
    body = (f"Fleet: #{issue.vehicle.vehicle_number} — {issue.title} "
            f"({issue.get_severity_display().lower()}). Reported by {who}.")
    if issue.details:
        body += f" {issue.details[:120]}"
    return body[:320]


def notify_issue_reported(issue):
    return _send_all(issue_text(issue))


def digest_text(attention, today) -> str:
    """The morning line. Empty when there is nothing in the NOW group."""
    now_items = attention.get("now") or []
    if not now_items:
        return ""
    counts = attention.get("counts", {})
    head = (f"Fleet {strf(today, '%a %b %-d')}: {len(now_items)} to look at now"
            f", {len(attention.get('week') or [])} this week.")
    lines = []
    for it in now_items[:DIGEST_MAX_LINES]:
        lines.append(f"• {it['title']}")
    if len(now_items) > DIGEST_MAX_LINES:
        lines.append(f"• +{len(now_items) - DIGEST_MAX_LINES} more on the Fleet desk")
    _ = counts
    return (head + "\n" + "\n".join(lines))[:600]


def send_digest(attention, today):
    body = digest_text(attention, today)
    if not body:
        return {"sent": 0, "skipped": "quiet"}
    return _send_all(body)
