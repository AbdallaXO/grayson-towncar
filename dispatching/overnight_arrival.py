"""
Overnight-arrival date confirmation (12 AM–6 AM pickups).

THE PROBLEM: a red-eye lands just after midnight, and the same flight number
lands at the same time every night. A guest booking a "June 3, 12:20 AM"
pickup may be on the flight that takes off June 2 (lands June 3 — booking
correct) or the one that takes off June 3 (lands June 4 — booking off by a
day). No flight lookup can tell them apart; only the guest's TAKEOFF date
(printed on their ticket) pins which instance they're on. Dispatchers used to
call every overnight guest to ask exactly that question — this module
automates the question.

Surfaces:
- Booking form: a gated popup asks the takeoff date at booking time and this
  module's derive helper turns it into the authoritative landing/pickup date
  (see overnight_views.overnight_flight_check).
- Backstop sweep (`overnight_confirm_sweep`): scheduled scan of upcoming
  overnight arrival legs that were never confirmed (phone/agent bookings) —
  sends a one-tap confirmation email through the existing verification
  machinery; only non-responders end up as a human call.

Gate: ONLY legs whose pickup time falls in [OVERNIGHT_START_HOUR,
OVERNIGHT_END_HOUR) AND that are flight-tracked arrivals with a usable flight
ident. Everyone else never sees any of this.
"""

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.signing import TimestampSigner
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Pickup times in [start, end) count as "overnight" — founder rule 2026-07-02:
# 12 AM through 5–6 AM max. Single knob; tighten END to 5 if the 5 AM band
# proves to never be ambiguous in practice.
OVERNIGHT_START_HOUR = 0
OVERNIGHT_END_HOUR = 6

# The dashboard's "After midnight" tail (next-day jobs shown at the end of
# TONIGHT's board) uses a narrower window — founder rule 2026-07-02: only
# 12 AM-2 AM jobs belong to the previous night's crew; anything later is the
# next morning's business.
NIGHT_TAIL_END_HOUR = 2

# Sweep configuration. Look-ahead starts TOMORROW (day-of changes are the
# dispatcher's call, not an automated email) and each leg is asked exactly
# once (stamped via overnight_confirm_sent_at).
SWEEP_DAYS_AHEAD = 14
SWEEP_MAX_LEGS_PER_RUN = 15  # AeroAPI budget guard: ≤2 schedule calls per leg

_SIGNER_SALT = "dispatching.overnight_arrival"
OVERNIGHT_TOKEN_MAX_AGE_SECONDS = 14 * 24 * 3600

_FROM_EMAIL = "reservations@graysontowncar.com"
_REPLY_TO = "reservations@graysontowncar.com"
_OFFICE_NOTIFY_EMAIL = "reservations@graysontowncar.com"

_EASTERN = ZoneInfo("America/New_York")


# ── Gates ────────────────────────────────────────────────────────────────────

def is_overnight_pickup_time(t) -> bool:
    """True when a pickup time falls in the ambiguous after-midnight window."""
    if t is None:
        return False
    return OVERNIGHT_START_HOUR <= t.hour < OVERNIGHT_END_HOUR


def leg_in_overnight_window(leg) -> bool:
    """The hard gate every overnight surface shares: after-midnight pickup AND
    a flight-tracked arrival with a usable flight ident. A 12:30 AM pickup with
    no flight, a 2 PM arrival, a 4 AM hotel→airport departure — all excluded."""
    if not is_overnight_pickup_time(getattr(leg, "pickup_time", None)):
        return False
    if not leg.is_flight_tracked_arrival():
        return False
    flight = getattr(leg, "flight_information", None)
    if flight is None or not flight.get_flight_ident():
        return False
    return True


def leg_needs_overnight_confirmation(leg) -> bool:
    """In the window and nobody (guest, staff) has confirmed which night yet."""
    if getattr(leg, "overnight_confirmed_at", None):
        return False
    return leg_in_overnight_window(leg)


# ── Derive: takeoff date → landing (the midnight math, done by us) ──────────

def derive_arrival_for_takeoff(airline_raw, flight_number_raw, takeoff_date):
    """
    Given the guest's airline + flight number + TAKEOFF date, return what that
    flight actually does. Returns a dict:

      {"status": "found", "arrival_local": aware Eastern dt, "arrival_date": date,
       "flight_label": "Delta 123", "flight_ident": "DAL123",
       "origin": "JFK", "destination": "MCO"}

      {"status": "not_found_on_date" | "not_found" | "not_orlando" |
       "rate_limited" | "bad_airline" | "error", "error": human message}

    "not_found_on_date" means AeroAPI knows the flight but it does NOT take off
    on the stated date (get_scheduled_flight silently falls back to the next
    available departure — we verify the returned departure date and refuse the
    fallback, because a fallback instance is exactly the wrong-night trap this
    whole feature exists to avoid).
    """
    from reservations.utils import (
        normalize_airline,
        normalize_flight_number,
        get_flightaware_code,
        get_airline_display_name,
    )
    from .aeroapi_service import AeroAPIService

    raw_airline = (airline_raw or "").strip()
    flight_digits = normalize_flight_number((flight_number_raw or "").strip()) or ""
    if not raw_airline or not flight_digits:
        return {"status": "bad_airline", "error": "Missing airline or flight number."}

    iata_code = normalize_airline(raw_airline)
    if not iata_code or len(iata_code) > 3 or not iata_code.isalnum():
        return {
            "status": "bad_airline",
            "error": f'Airline "{raw_airline}" isn\'t recognized.',
        }
    airline_display = get_airline_display_name(iata_code) or iata_code
    fa_code = get_flightaware_code(iata_code) or iata_code
    flight_ident = f"{fa_code}{flight_digits}"
    label = f"{airline_display} {flight_digits}".strip()

    aeroapi = AeroAPIService()
    try:
        data = aeroapi.get_scheduled_flight(
            flight_ident, takeoff_date.isoformat(), trip_type="arrival"
        )
    except Exception as e:
        logger.error(f"overnight derive: AeroAPI failed for {flight_ident}: {e}")
        return {"status": "error", "error": "Flight lookup failed."}

    status = data.get("status")
    if status == "rate_limited":
        return {"status": "rate_limited", "error": data.get("error", "Rate limited.")}
    if status == "not_orlando":
        return {"status": "not_orlando", "error": data.get("error", "")}
    if status != "success":
        return {"status": "not_found", "error": data.get("error", "Flight not found.")}

    # Refuse the next-available-departure fallback: the schedule we got back
    # must actually take off on the guest's stated date (Eastern).
    dep_local = data.get("scheduled_departure_local")
    if dep_local is not None:
        dep_eastern = (
            dep_local.astimezone(_EASTERN)
            if timezone.is_aware(dep_local)
            else dep_local.replace(tzinfo=_EASTERN)
        )
        if dep_eastern.date() != takeoff_date:
            return {
                "status": "not_found_on_date",
                "error": (
                    f"{label} doesn't appear to take off on "
                    f"{takeoff_date.strftime('%b %d, %Y')}."
                ),
            }

    arrival = data.get("scheduled_gate_arrival_local") or data.get("scheduled_arrival_local")
    if arrival is None:
        return {"status": "not_found", "error": "No arrival time available."}
    arrival_eastern = (
        arrival.astimezone(_EASTERN)
        if timezone.is_aware(arrival)
        else arrival.replace(tzinfo=_EASTERN)
    )

    return {
        "status": "found",
        "arrival_local": arrival_eastern,
        "arrival_date": arrival_eastern.date(),
        "flight_label": label,
        "flight_ident": flight_ident,
        "origin": (data.get("origin") or "").strip(),
        "destination": (data.get("destination") or "").strip(),
    }


# ── Stamping ─────────────────────────────────────────────────────────────────

def stamp_overnight_confirmed(leg, takeoff_date, source):
    """Record that the overnight night-of question is answered: persist the
    takeoff date on the Flight (future AeroAPI lookups anchor on it) and the
    confirmation stamp on the Leg. Queryset update — same no-signal pattern as
    flight_verification_email_sent_at."""
    from reservations.models import Leg as _Leg

    flight = getattr(leg, "flight_information", None)
    if flight is not None and takeoff_date is not None:
        flight.departure_date = takeoff_date
        flight.save(update_fields=["departure_date"])

    now = timezone.now()
    _Leg.objects.filter(pk=leg.pk).update(
        overnight_confirmed_at=now,
        overnight_confirmed_source=source,
    )
    leg.overnight_confirmed_at = now
    leg.overnight_confirmed_source = source


def apply_overnight_answer(leg, choice, source, resolved_by=None, notify_office=True):
    """
    Commit a takeoff-date answer for an overnight leg, whoever gave it.

    choice='prev'  — guest takes off the night BEFORE the booked date →
                     lands on the booked date, pickup stays put.
    choice='same'  — guest takes off ON the booked date → lands (and the
                     pickup moves to) the day after.

    Shared by the guest one-tap page (source='one_tap', office notified on a
    move) and the dispatcher board buttons (source='staff', no office email —
    the dispatcher IS the office). Closes any open FLIGHT_VERIFICATION task.
    Returns {"moved", "takeoff", "old_pickup_date", "new_pickup_date"}.
    """
    from reservations.models import Leg as _Leg

    old_pickup_date = leg.pickup_date
    moved = False

    if choice == "prev":
        takeoff = old_pickup_date - timedelta(days=1)
    else:
        takeoff = old_pickup_date
        new_pickup_date = old_pickup_date + timedelta(days=1)
        _Leg.objects.filter(pk=leg.pk).update(pickup_date=new_pickup_date)
        leg.pickup_date = new_pickup_date
        moved = True

    stamp_overnight_confirmed(leg, takeoff, source=source)

    if moved and notify_office:
        try:
            notify_office_pickup_moved(
                leg, old_pickup_date, leg.pickup_date, source_label="one-tap email"
            )
        except Exception as e:
            logger.error(f"overnight answer: office notify failed leg {leg.id}: {e}")

    try:
        from ops.models import OperationalTask
        from ops.services import close_task

        note = (
            f"Takeoff {fmt_date(takeoff)} confirmed via {source}"
            + (
                f" — pickup MOVED {fmt_date(old_pickup_date)} → {fmt_date(leg.pickup_date)}"
                if moved
                else " — booked pickup date correct"
            )
        )
        open_tasks = OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        for task in open_tasks:
            close_task(task, resolved_by=resolved_by, resolution_notes=note)
    except Exception as e:
        logger.warning(f"overnight answer: close-task failed leg {leg.id}: {e}")

    return {
        "moved": moved,
        "takeoff": takeoff,
        "old_pickup_date": old_pickup_date,
        "new_pickup_date": leg.pickup_date,
    }


# ── One-tap tokens ───────────────────────────────────────────────────────────

def make_overnight_token(leg_id: int) -> str:
    return TimestampSigner(salt=_SIGNER_SALT).sign(str(int(leg_id)))


def parse_overnight_token(token: str) -> int:
    """Returns leg_id or raises BadSignature/SignatureExpired."""
    raw = TimestampSigner(salt=_SIGNER_SALT).unsign(
        token, max_age=OVERNIGHT_TOKEN_MAX_AGE_SECONDS
    )
    return int(raw)


def _confirm_url(token: str, choice: str = "", request=None) -> str:
    path = reverse("overnight_confirm_public", args=[token])
    if choice:
        path = f"{path}?choice={choice}"
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


# ── Formatting (Windows-safe: no %-d / %-I) ─────────────────────────────────

def fmt_date(d) -> str:
    return f"{d.strftime('%a, %b')} {d.day}" if d else ""


def fmt_time(t) -> str:
    return t.strftime("%I:%M %p").lstrip("0") if t else ""


# ── One-tap confirmation email ───────────────────────────────────────────────

def build_overnight_email_context(leg, derived=None, request=None):
    """Context shared by the email template and (partly) the public page.
    prev_day = takeoff the night before → lands on the booked pickup date
    (booking correct). same_day = takeoff on the booked date → lands the day
    AFTER (pickup moves +1)."""
    reservation = getattr(leg, "reservation", None)
    customer = getattr(reservation, "customer", None) if reservation else None
    flight = getattr(leg, "flight_information", None)

    pickup_date = leg.pickup_date
    prev_day = pickup_date - timedelta(days=1)
    next_day = pickup_date + timedelta(days=1)
    token = make_overnight_token(leg.id)

    airline_display = (
        getattr(flight, "airline_display_name", "") or getattr(flight, "airline", "") or ""
    ) if flight else ""
    flight_number = getattr(flight, "flight_number", "") if flight else ""

    return {
        "customer_first_name": (getattr(customer, "first_name", "") or "").strip().title() or "there",
        "reservation": reservation,
        "leg": leg,
        "flight_label": f"{airline_display} {flight_number}".strip() or "your flight",
        "pickup_date": pickup_date,
        "pickup_time": leg.pickup_time,
        "pickup_date_str": fmt_date(pickup_date),
        "pickup_time_str": fmt_time(leg.pickup_time),
        "prev_day_str": fmt_date(prev_day),
        "same_day_str": fmt_date(pickup_date),
        "next_day_str": fmt_date(next_day),
        "confirm_prev_url": _confirm_url(token, "prev", request=request),
        "confirm_same_url": _confirm_url(token, "same", request=request),
        "confirm_url": _confirm_url(token, request=request),
        # When the sweep pre-checked AeroAPI, show the derived facts.
        "derived_found": bool(derived and derived.get("status") == "found"),
        "derived_arrival_date_str": fmt_date(derived["arrival_date"]) if derived and derived.get("status") == "found" else "",
        "derived_arrival_time_str": fmt_time(derived["arrival_local"].time()) if derived and derived.get("status") == "found" else "",
        "reply_to": _REPLY_TO,
    }


def send_overnight_confirm_email(leg, derived=None, request=None) -> dict:
    """Send the one-tap 'which night do you take off?' email. SYNCHRONOUS —
    callers are the background sweep (already off the request path) and tests.
    Stamps overnight_confirm_sent_at on success."""
    from reservations.models import Leg as _Leg

    reservation = getattr(leg, "reservation", None)
    customer = getattr(reservation, "customer", None) if reservation else None
    to_email = (getattr(customer, "email", "") or "").strip()
    if not to_email:
        return {"success": False, "error": "Guest has no email on file."}
    if not leg.pickup_date:
        return {"success": False, "error": "Leg has no pickup date."}

    ctx = build_overnight_email_context(leg, derived=derived, request=request)
    subject = (
        f"Which night do you land? Your {ctx['pickup_time_str']} pickup on "
        f"{ctx['pickup_date_str']}"
    )
    html = render_to_string("users/overnight_confirm_email.html", ctx)

    msg = EmailMultiAlternatives(
        subject=subject,
        body="",
        from_email=_FROM_EMAIL,
        to=[to_email],
        reply_to=[_REPLY_TO],
    )
    msg.attach_alternative(html, "text/html")
    try:
        msg.send()
    except Exception as e:
        logger.error(f"overnight confirm email failed for leg {leg.id}: {e}")
        return {"success": False, "error": str(e)}

    sent_at = timezone.now()
    _Leg.objects.filter(pk=leg.pk).update(overnight_confirm_sent_at=sent_at)
    leg.overnight_confirm_sent_at = sent_at

    try:
        from users.emails import log_email_sent
        log_email_sent(
            email_type="overnight_date_confirm",
            recipient_email=to_email,
            subject=subject,
            reservation=reservation,
            metadata={"leg_id": leg.id},
        )
    except Exception as log_err:
        logger.warning(f"overnight confirm email-log write failed: {log_err}")

    logger.info(f"overnight confirm email sent for leg={leg.id} to={to_email}")
    return {"success": True, "recipient": to_email}


def notify_office_pickup_moved(leg, old_pickup_date, new_pickup_date, source_label):
    """Short office heads-up when a guest's one-tap answer MOVED the pickup
    date. Plain email into the existing dispatch inbox — loud enough that a
    date change never lands silently."""
    reservation = getattr(leg, "reservation", None)
    customer = getattr(reservation, "customer", None) if reservation else None
    full_name = (
        f"{(getattr(customer, 'first_name', '') or '').strip()} "
        f"{(getattr(customer, 'last_name', '') or '').strip()}"
    ).strip() or "Guest"
    res_no = f"#50{reservation.id}" if reservation else "?"

    subject = (
        f"PICKUP DATE MOVED by guest: {full_name} {res_no} — "
        f"{fmt_date(old_pickup_date)} → {fmt_date(new_pickup_date)}"
    )
    body = (
        f"{full_name} answered the overnight date-confirmation ({source_label}).\n\n"
        f"They take off on {fmt_date(old_pickup_date)}, so their flight lands after "
        f"midnight on {fmt_date(new_pickup_date)}.\n\n"
        f"Pickup moved: {fmt_date(old_pickup_date)} → {fmt_date(new_pickup_date)} "
        f"at {fmt_time(leg.pickup_time)}.\n"
        f"Leg ID: {leg.id}\n\n"
        f"The dispatch board for both dates should be rechecked."
    )
    try:
        msg = EmailMultiAlternatives(
            subject=subject, body=body, from_email=_FROM_EMAIL, to=[_OFFICE_NOTIFY_EMAIL]
        )
        msg.send()
    except Exception as e:
        logger.error(f"overnight office notify failed for leg {leg.id}: {e}")


# ── Backstop sweep ───────────────────────────────────────────────────────────

def overnight_confirm_sweep(days_ahead=SWEEP_DAYS_AHEAD, max_legs=SWEEP_MAX_LEGS_PER_RUN):
    """
    Scan upcoming overnight arrival legs that nobody has confirmed and send
    each guest the one-tap takeoff-date question (once, ever — stamped).

    Per leg, one or two /schedules calls pre-check what AeroAPI knows:
      - flight takes off pickup_date-1 and lands pickup_date → booking looks
        consistent, but the classic off-by-one is still possible → ask anyway,
        with the derived facts in the email.
      - only the pickup_date takeoff exists (lands pickup_date+1) → booking is
        very likely off by one → same question; guest's answer moves the date.
      - AeroAPI can't find the flight at all / wrong airport → hand off to the
        existing "we can't find your flight" verification email instead.
    Guests with no email get an ops task so the desk calls only those few.
    """
    from reservations.models import Leg
    from ops.models import OperationalTask
    from ops.services import create_task

    today = timezone.localdate()
    start = today + timedelta(days=1)
    end = today + timedelta(days=days_ahead)

    candidates = (
        Leg.objects.filter(
            pickup_date__gte=start,
            pickup_date__lte=end,
            pickup_time__gte=time(OVERNIGHT_START_HOUR, 0),
            pickup_time__lt=time(OVERNIGHT_END_HOUR, 0),
            flight_information__isnull=False,
            overnight_confirmed_at__isnull=True,
            overnight_confirm_sent_at__isnull=True,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("reservation__customer", "flight_information")
        .order_by("pickup_date", "pickup_time")
    )

    sent = 0
    tasks = 0
    skipped = 0
    processed = 0

    for leg in candidates:
        if processed >= max_legs:
            logger.info(
                f"overnight sweep: hit per-run cap ({max_legs}), remaining legs "
                f"picked up next cycle"
            )
            break
        if not leg_needs_overnight_confirmation(leg):
            skipped += 1
            continue
        processed += 1

        flight = leg.flight_information
        prev_day = leg.pickup_date - timedelta(days=1)

        derived = derive_arrival_for_takeoff(
            flight.airline, flight.flight_number, prev_day
        )
        if derived.get("status") == "rate_limited":
            logger.warning("overnight sweep: AeroAPI rate-limited, aborting this run")
            break

        scenario = "ambiguous"
        if derived.get("status") == "found" and derived["arrival_date"] == leg.pickup_date:
            scenario = "consistent"
        elif derived.get("status") in ("not_found", "not_found_on_date"):
            # No prev-day takeoff. If the flight takes off ON the pickup date
            # instead, the booking is very likely off by one.
            same_day_try = derive_arrival_for_takeoff(
                flight.airline, flight.flight_number, leg.pickup_date
            )
            if same_day_try.get("status") == "rate_limited":
                logger.warning("overnight sweep: AeroAPI rate-limited, aborting this run")
                break
            if same_day_try.get("status") == "found":
                scenario = "likely_wrong_date"
                derived = None  # prev-day facts don't exist; don't show them
            else:
                scenario = "flight_not_found"
                derived = None
        elif derived.get("status") == "not_orlando":
            scenario = "flight_not_found"
            derived = None
        elif derived.get("status") == "found":
            # Found a prev-day takeoff but it doesn't land on the pickup date
            # (e.g. lands late evening prev-day). Odd — ask without facts.
            derived = None

        customer = getattr(leg.reservation, "customer", None)
        to_email = (getattr(customer, "email", "") or "").strip()

        if scenario == "flight_not_found":
            # Wrong/old flight number territory — the existing verification
            # flow owns this. Stamp so we don't re-derive every cycle.
            try:
                from .flight_verify_email import send_flight_verification_email
                if to_email:
                    send_flight_verification_email(leg, sent_by=None)
                task = create_task(
                    task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                    title=(
                        f"Overnight pickup, flight not found: "
                        f"{fmt_date(leg.pickup_date)} {fmt_time(leg.pickup_time)}"
                    )[:200],
                    description=(
                        f"Overnight arrival ({fmt_time(leg.pickup_time)}) on "
                        f"{fmt_date(leg.pickup_date)} — AeroAPI can't find "
                        f"{flight.get_flight_ident() or 'the flight'} taking off "
                        f"{fmt_date(prev_day)} or {fmt_date(leg.pickup_date)}. "
                        f"Flight number is likely wrong AND the date is unconfirmed. "
                        f"{'Verification email sent.' if to_email else 'Guest has NO EMAIL — call to confirm.'}"
                    ),
                    priority=OperationalTask.Priority.HIGH,
                    due_at=timezone.now(),
                    leg=leg,
                    reservation=leg.reservation,
                    metadata={"source": "overnight_sweep", "scenario": scenario},
                )
                if task:
                    tasks += 1
                from reservations.models import Leg as _Leg
                _Leg.objects.filter(pk=leg.pk).update(
                    overnight_confirm_sent_at=timezone.now()
                )
            except Exception as e:
                logger.error(f"overnight sweep: not-found handoff failed leg {leg.id}: {e}")
            continue

        if not to_email:
            # No email → this one really is a phone call, but only this one.
            task = create_task(
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                title=(
                    f"Call to confirm overnight date: "
                    f"{fmt_date(leg.pickup_date)} {fmt_time(leg.pickup_time)}"
                )[:200],
                description=(
                    f"Overnight arrival {fmt_time(leg.pickup_time)} on "
                    f"{fmt_date(leg.pickup_date)} ({flight.get_flight_ident() or 'no ident'}). "
                    f"Guest has no email for the one-tap confirmation — call and ask "
                    f"which date they TAKE OFF: {fmt_date(prev_day)} (pickup correct) "
                    f"or {fmt_date(leg.pickup_date)} (pickup moves to "
                    f"{fmt_date(leg.pickup_date + timedelta(days=1))})."
                ),
                priority=OperationalTask.Priority.MEDIUM,
                due_at=timezone.now(),
                leg=leg,
                reservation=leg.reservation,
                metadata={"source": "overnight_sweep", "scenario": scenario},
            )
            if task:
                tasks += 1
            from reservations.models import Leg as _Leg
            _Leg.objects.filter(pk=leg.pk).update(
                overnight_confirm_sent_at=timezone.now()
            )
            continue

        result = send_overnight_confirm_email(leg, derived=derived)
        if result.get("success"):
            sent += 1
            # Open a tracking task so unanswered one-taps surface to the desk
            # instead of evaporating. Closed automatically when the guest taps.
            task = create_task(
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                title=(
                    f"Overnight date confirmation pending: "
                    f"{fmt_date(leg.pickup_date)} {fmt_time(leg.pickup_time)}"
                )[:200],
                description=(
                    f"One-tap takeoff-date email sent for the overnight arrival "
                    f"{fmt_time(leg.pickup_time)} on {fmt_date(leg.pickup_date)} "
                    f"({flight.get_flight_ident() or 'no ident'}"
                    f"{', likely off-by-one: only a ' + fmt_date(leg.pickup_date) + ' takeoff exists' if scenario == 'likely_wrong_date' else ''}). "
                    f"Auto-closes when the guest answers. If still open the day "
                    f"before pickup, call to confirm."
                ),
                priority=(
                    OperationalTask.Priority.HIGH
                    if scenario == "likely_wrong_date"
                    else OperationalTask.Priority.LOW
                ),
                due_at=timezone.now() + timedelta(hours=48),
                leg=leg,
                reservation=leg.reservation,
                metadata={"source": "overnight_sweep", "scenario": scenario},
            )
            if task:
                tasks += 1

    if sent or tasks:
        logger.info(
            f"overnight sweep: {sent} one-tap emails, {tasks} tasks, {skipped} skipped"
        )
    return {"sent": sent, "tasks": tasks, "skipped": skipped, "processed": processed}
