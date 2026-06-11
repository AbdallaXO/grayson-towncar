"""Early-morning wake-up checks: make sure a driver with a pre-dawn first
pickup is actually awake, and put the owners in the loop while there is still
time to react.

The ladder (founder-set 2026-06-11), all relative to the driver's FIRST
pickup time T that day:

  T-90   wake-up SMS with a tap-to-confirm link (+ web push if subscribed)
  T-55   no ack -> automated phone call ("press any key to confirm");
         T-90 instead when the first job is a far/cruise run (Port Canaveral,
         Sanford, far/odd addresses) because the driver leaves home earlier —
         the SMS then moves up too so it still precedes the call
  T-50   still no ack -> CALL + TEXT every owner (WAKEUP_NOTIFY_PHONES)

Acks the app can see: tapping the tokenized link, or pressing a digit on the
call. SMS *replies* are NOT monitored — the Twilio number is GHL-managed, so
inbound messages land in the GHL inbox, never here.

Step timestamps mean "attempted at": the ladder is time-driven and a failed
Twilio send never blocks the next rung (failures are appended to check.log).
A check created late (leg assigned/retimed inside the window) ramps the same
three steps spaced WAKEUP_MIN_STEP_GAP_MIN apart instead of firing them all
at once.

Everything is driven by run_wakeup_cycle(), called every minute by
drivers/wakeup_scheduler.py and manually via `manage.py wakeup_cycle`.
Inert unless settings.WAKEUP_CHECKS_ENABLED.
"""
import logging
from datetime import datetime, time, timedelta
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone

from drivers.timeoff_notifications import _normalize_e164, _send, _twilio_client

logger = logging.getLogger(__name__)

# Steps never fire once the pickup is this far in the past (server downtime
# must not produce a noon "wake-up" call for a 6 AM job).
GRACE_MIN = 5


def wakeup_enabled():
    return bool(settings.WAKEUP_CHECKS_ENABLED)


def _fmt_time(dt):
    """5:30 AM — local, no platform-specific strftime flags (Windows dev)."""
    return timezone.localtime(dt).strftime("%I:%M %p").lstrip("0")


def _log(check, message, now=None):
    stamp = _fmt_time(now or timezone.now())
    check.log = (check.log + f"[{stamp}] {message}\n") if check.log else f"[{stamp}] {message}\n"


def _confirm_url(check):
    return f"{settings.SITE_BASE_URL}/drivers/wakeup/{check.token}/"


def _pickup_dt(leg):
    return timezone.make_aware(datetime.combine(leg.pickup_date, leg.pickup_time))


def is_far_job(leg):
    """Far/cruise first job -> the wake-up call moves up to T-WAKEUP_CALL_LEAD_FAR_MIN."""
    from dispatching.analytics import categorize_location

    far = set(settings.WAKEUP_FAR_CATEGORIES)
    return (
        categorize_location(leg.pickup_location or "") in far
        or categorize_location(leg.dropoff_location or "") in far
    )


def _call_lead(check):
    minutes = (
        settings.WAKEUP_CALL_LEAD_FAR_MIN
        if check.leg is not None and is_far_job(check.leg)
        else settings.WAKEUP_CALL_LEAD_MIN
    )
    return timedelta(minutes=minutes)


def _sms_lead(check):
    """The text always precedes the call by at least the step gap (far jobs:
    call T-90 pulls the text up to T-100)."""
    floor = _call_lead(check) + timedelta(minutes=settings.WAKEUP_MIN_STEP_GAP_MIN)
    return max(timedelta(minutes=settings.WAKEUP_SMS_LEAD_MIN), floor)


# ── Outbound (patch points for tests) ────────────────────────────────────


def _send_sms(to_number, body):
    return _send(to_number, body)


def _place_call(to_number, twiml):
    """Outbound Twilio voice call. Returns (ok, error)."""
    client, from_number = _twilio_client()
    if client is None:
        return False, "twilio not configured"
    to = _normalize_e164(to_number)
    if not to:
        return False, "missing recipient"
    try:
        client.calls.create(to=to, from_=from_number, twiml=twiml)
        return True, None
    except Exception as e:
        logger.exception("Wake-up call to %s failed", to)
        return False, str(e)


def say_xml(text, loop=None):
    """<Say> element in the configured voice (settings.WAKEUP_VOICE — the
    generative tier reads far more naturally than Twilio's default robot).
    Escapes the text; empty WAKEUP_VOICE falls back to Twilio's default."""
    voice = (getattr(settings, "WAKEUP_VOICE", "") or "").strip()
    attrs = f' voice="{escape(voice)}"' if voice else ""
    if loop:
        attrs += f' loop="{loop}"'
    return f"<Say{attrs}>{escape(text)}</Say>"


# Kept simple on purpose (founder 2026-06-11: tried a rotating-quotes
# sign-off, decided against it).
def confirm_line():
    return "Great — you're confirmed. Have a great day."


def _send_push(check):
    from drivers.push import push_enabled, send_push_to_driver

    if not push_enabled():
        return 0
    return send_push_to_driver(
        check.driver_id,
        "Wake-up check",
        f"First pickup {_fmt_time(check.first_pickup_at)} — tap to confirm you're up.",
        url=f"/drivers/wakeup/{check.token}/",
        tag="gt-wakeup",
    )


# ── Steps ────────────────────────────────────────────────────────────────


def _driver_name(check):
    name = check.driver.profile.get_full_name() if check.driver.profile_id else ""
    return name or str(check.driver)


def _first_name(check):
    first = check.driver.profile.first_name if check.driver.profile_id else ""
    return (first or _driver_name(check)).strip()


def _step_sms(check, now):
    body = (
        f"Good morning {_driver_name(check)} — Grayson Towncar wake-up check.\n"
        f"Your first pickup is at {_fmt_time(check.first_pickup_at)}.\n"
        f"Tap to confirm you're up: {_confirm_url(check)}\n"
        f"(Replies to this number aren't monitored — use the link.)"
    )
    phone = (check.driver.phone_number or "").strip()
    if phone:
        ok, err = _send_sms(phone, body)
        _log(check, "wake-up SMS sent" if ok else f"wake-up SMS FAILED: {err}", now)
    else:
        _log(check, "wake-up SMS skipped: driver has no phone number on file", now)
    check.sms_sent_at = now
    try:
        if _send_push(check):
            check.push_sent_at = now
            _log(check, "web push sent", now)
    except Exception as e:  # push must never break the ladder
        logger.warning("Wake-up push for check %s errored: %s", check.pk, e)


def _step_call(check, now):
    phone = (check.driver.phone_number or "").strip()
    if not phone:
        _log(check, "wake-up call skipped: driver has no phone number on file", now)
        check.call_started_at = now
        return
    # "in about 50 minutes" — live countdown, rounded to 5 so it reads naturally.
    mins = max(0, int(round((check.first_pickup_at - now).total_seconds() / 60)))
    when = f"in about {5 * round(mins / 5)} minutes" if mins >= 5 else "very soon"
    say = (
        f"Good morning {_first_name(check)}. This is the Grayson Towncar A.I. "
        f"assistant — just calling to confirm you're awake for your pickup {when}, "
        f"at {_fmt_time(check.first_pickup_at)}. "
        f"Please say yes, or press any key, to confirm."
    )
    # Once through, a beat of silence, then a short nudge — never the full
    # message twice back-to-back (founder note 2026-06-11).
    reprompt = "Are you up? Just say yes, or press any key."
    gather_url = escape(f"{settings.SITE_BASE_URL}/drivers/wakeup/{check.token}/gather/")
    twiml = (
        "<Response>"
        f'<Gather action="{gather_url}" method="POST" input="dtmf speech" '
        f'numDigits="1" timeout="15" speechTimeout="auto" '
        f'hints="yes, yeah, yep, I am up, awake">'
        f"{say_xml(say)}"
        '<Pause length="4"/>'
        f"{say_xml(reprompt)}"
        "</Gather>"
        f"{say_xml('We did not get a response. Please call dispatch. Goodbye.')}"
        "</Response>"
    )
    ok, err = _place_call(phone, twiml)
    _log(check, "wake-up call placed" if ok else f"wake-up call FAILED: {err}", now)
    check.call_started_at = now


def _step_escalate(check, now):
    name = _driver_name(check)
    pickup = _fmt_time(check.first_pickup_at)
    mins_away = max(0, int((check.first_pickup_at - now).total_seconds() // 60))
    attempts = []
    if check.sms_sent_at:
        attempts.append(f"text {_fmt_time(check.sms_sent_at)}")
    if check.call_started_at:
        attempts.append(f"call {_fmt_time(check.call_started_at)}")
    attempted = " + ".join(attempts) if attempts else "no contact possible (no phone on file)"
    body = (
        f"WAKE-UP ALERT: {name} has NOT confirmed he's awake.\n"
        f"First pickup {pickup} ({mins_away} min away).\n"
        f"Attempted: {attempted} — no response."
    )
    say = (
        f"Grayson Towncar alert. {name} has not confirmed his {pickup} wake-up check. "
        f"Please reach him now."
    )
    twiml = f"<Response>{say_xml(say, loop=2)}</Response>"
    for phone in settings.WAKEUP_NOTIFY_PHONES:
        sms_ok, sms_err = _send_sms(phone, body)
        call_ok, call_err = _place_call(phone, twiml)
        _log(
            check,
            f"owner {phone}: text {'sent' if sms_ok else f'FAILED ({sms_err})'}, "
            f"call {'placed' if call_ok else f'FAILED ({call_err})'}",
            now,
        )
    check.escalated_at = now


def ack_check(check, source, now=None):
    """Record that the driver confirmed he's awake. Idempotent. If the owners
    were already alerted, text them the all-clear."""
    if check.acked_at:
        return check
    now = now or timezone.now()
    check.acked_at = now
    check.ack_source = source
    check.status = check.STATUS_ACKED
    _log(check, f"CONFIRMED awake via {source}", now)
    was_escalated = bool(check.escalated_at)
    check.save(update_fields=["acked_at", "ack_source", "status", "log"])
    if was_escalated:
        body = (
            f"All clear — {_driver_name(check)} just confirmed he's up "
            f"({_fmt_time(now)}). First pickup {_fmt_time(check.first_pickup_at)}."
        )
        for phone in settings.WAKEUP_NOTIFY_PHONES:
            _send_sms(phone, body)
    return check


# ── Sweep ────────────────────────────────────────────────────────────────


def _eligible_first_legs(dates):
    """{(driver_id, date): first Leg} for in-house drivers whose first pickup
    of that day is before the early cutoff."""
    from reservations.models import Leg

    cutoff = time(settings.WAKEUP_EARLY_CUTOFF_HOUR, 0)
    legs = (
        Leg.objects.filter(pickup_date__in=dates, driver__driver_type="inhouse")
        .exclude(status__in=["completed", "cancelled"])
        .exclude(pickup_time__isnull=True)
        .select_related("driver", "driver__profile")
        .order_by("pickup_date", "driver_id", "pickup_time")
    )
    first = {}
    for leg in legs:
        first.setdefault((leg.driver_id, leg.pickup_date), leg)
    return {k: leg for k, leg in first.items() if leg.pickup_time < cutoff}


def _sync_checks(eligible, dates, now):
    """Create checks entering their window, retime ones whose first leg moved,
    cancel ones whose driver no longer has an early first pickup (and revive
    them if he gets one back)."""
    from drivers.models import DriverWakeupCheck

    grace = timedelta(minutes=GRACE_MIN)
    existing = {
        (c.driver_id, c.date): c
        for c in DriverWakeupCheck.objects.filter(date__in=dates).select_related(
            "driver", "driver__profile", "leg"
        )
    }
    created = updated = cancelled = 0

    for key, leg in eligible.items():
        t = _pickup_dt(leg)
        check = existing.get(key)
        if check is None:
            # Create only once inside the action window — no point holding a
            # row for tomorrow 5 AM at noon today.
            probe = DriverWakeupCheck(leg=leg, first_pickup_at=t)
            if now >= t - _sms_lead(probe) and now <= t + grace:
                DriverWakeupCheck.objects.create(
                    driver_id=leg.driver_id, date=leg.pickup_date,
                    leg=leg, first_pickup_at=t,
                )
                created += 1
            continue
        if check.status == check.STATUS_ACKED:
            continue  # he's awake — board churn afterwards doesn't un-ring that bell
        fields = []
        if check.first_pickup_at != t or check.leg_id != leg.id:
            check.first_pickup_at = t
            check.leg = leg
            fields += ["first_pickup_at", "leg"]
        if check.status == check.STATUS_CANCELLED:
            check.status = check.STATUS_PENDING
            _log(check, "revived — early first pickup is back", now)
            fields += ["status", "log"]
        if fields:
            check.save(update_fields=sorted(set(fields)))
            updated += 1

    for key, check in existing.items():
        if key in eligible:
            continue
        if check.status in (check.STATUS_PENDING, check.STATUS_ESCALATED):
            check.status = check.STATUS_CANCELLED
            _log(check, "cancelled — no longer an early first pickup", now)
            check.save(update_fields=["status", "log"])
            cancelled += 1

    return created, updated, cancelled


def _advance_checks(dates, now):
    """Fire at most ONE due step per check per cycle (text → call → owners)."""
    from drivers.models import DriverWakeupCheck

    gap = timedelta(minutes=settings.WAKEUP_MIN_STEP_GAP_MIN)
    grace = timedelta(minutes=GRACE_MIN)
    steps = 0
    checks = (
        DriverWakeupCheck.objects.filter(
            date__in=dates,
            status__in=[DriverWakeupCheck.STATUS_PENDING, DriverWakeupCheck.STATUS_ESCALATED],
            acked_at__isnull=True,
        ).select_related("driver", "driver__profile", "leg")
    )
    for check in checks:
        t = check.first_pickup_at
        if now > t + grace:
            continue  # pickup has passed — never wake anyone retroactively
        sms_lead = _sms_lead(check)
        call_lead = _call_lead(check)
        esc_lead = timedelta(minutes=settings.WAKEUP_ESCALATE_LEAD_MIN)
        # A late-created check ramps its rungs at least `gap` apart — but the
        # gap may never exceed the ladder's OWN spacing (call→owners is only
        # 5 min apart by design), or on-schedule escalation would slip.
        gap_call = min(gap, sms_lead - call_lead)
        gap_esc = min(gap, call_lead - esc_lead)
        if check.sms_sent_at is None:
            if now >= t - sms_lead:
                _step_sms(check, now)
                check.save(update_fields=["sms_sent_at", "push_sent_at", "log"])
                steps += 1
        elif check.call_started_at is None:
            if now >= t - call_lead and now >= check.sms_sent_at + gap_call:
                _step_call(check, now)
                check.save(update_fields=["call_started_at", "log"])
                steps += 1
        elif check.escalated_at is None:
            if now >= t - esc_lead and now >= check.call_started_at + gap_esc:
                _step_escalate(check, now)
                check.status = check.STATUS_ESCALATED
                check.save(update_fields=["escalated_at", "status", "log"])
                steps += 1
    return steps


def run_wakeup_cycle(now=None):
    """One sweep: sync checks against the live board, then advance ladders.
    Never raises. `now` is injectable for tests / the management command."""
    if not wakeup_enabled():
        return {"status": "disabled"}
    now = now or timezone.now()
    today = timezone.localdate(now)
    # Tomorrow too: a 00:30 first pickup starts its ladder at ~23:00 tonight.
    dates = [today, today + timedelta(days=1)]
    try:
        eligible = _eligible_first_legs(dates)
        created, updated, cancelled = _sync_checks(eligible, dates, now)
        steps = _advance_checks(dates, now)
    except Exception:
        logger.exception("Wake-up cycle failed")
        return {"status": "error"}
    summary = {
        "status": "ok", "eligible": len(eligible), "created": created,
        "updated": updated, "cancelled": cancelled, "steps": steps,
    }
    if created or cancelled or steps:
        logger.info("Wake-up cycle: %s", summary)
    return summary
