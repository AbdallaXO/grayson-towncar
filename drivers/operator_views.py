"""Operator portal — the driver app as seen by a company that re-dispatches.

An operator is an affiliate whose login represents a COMPANY, not a person
(``drivers.Driver.portal_role == 'operator'``). We farm them a leg; they key it
into their own system (LimoAnywhere and friends) and send one of their drivers.
They never touch the wheel, so the chauffeur portal is the wrong tool: "I'm on
the way" is a lie in their mouth, and the one thing they do constantly — copy
the job into another system — the chauffeur portal doesn't do at all.

So this module serves the same legs with a different job:
  * a RESPONSE QUEUE of farm-outs they haven't accepted yet, across all dates,
    so nothing sits unanswered just because it isn't today;
  * accept / decline, where a decline hands the leg straight back to our board
    (unassigned + a KEOI watch flag + a text to dispatch) rather than dying in
    an inbox;
  * copy-out, per field and whole-job (``operator_jobs``);
  * "who did you put on it" — their chauffeur's name and cell on the leg, so our
    dispatcher calls the man on the job instead of relaying through the office.

Ownership is enforced exactly as the chauffeur portal does it — every lookup is
filtered by ``driver__profile=request.user``, so an operator can only ever act on
legs assigned to them. Status updates deliberately REUSE ``views.update_leg_status``
rather than forking a second write path.
"""

import json
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from reservations.models import Leg, LegKeoi, LegStatus

from .models import Driver
from .operator_jobs import build_day_text, build_job_fields, build_job_text, short_place

logger = logging.getLogger(__name__)

# How far ahead the response queue looks. Farm-outs land weeks out; an operator
# who can only see today would be answering everything late.
QUEUE_HORIZON_DAYS = 60

# Statuses an operator reports on behalf of their driver. Same vocabulary the
# chauffeur portal writes (one status field, one board), relabelled for someone
# reporting on a third party.
OPERATOR_STATUS_LABELS = [
    ("confirmed", "Accepted", "bi-check2"),
    ("on-the-way", "Driver on the way", "bi-arrow-right-circle"),
    ("on-location", "Driver on location", "bi-geo-alt"),
    ("picked-up", "Passenger on board", "bi-person-check"),
    ("completed", "Dropped off", "bi-flag"),
]

DECLINE_REASONS = [
    "No car available",
    "Already booked that time",
    "Too far / outside our area",
    "Rate doesn't work",
    "Other",
]


def get_operator(request):
    """The logged-in operator, or None if this login isn't one.

    Returns None rather than 404ing so callers can choose between redirecting a
    chauffeur back to their own portal and refusing an API call.
    """
    driver = Driver.objects.filter(profile=request.user).first()
    if driver and driver.is_operator:
        return driver
    return None


def _operator_or_403(request):
    operator = get_operator(request)
    if operator is None:
        return None, JsonResponse(
            {"success": False, "error": "Not an operator account"}, status=403
        )
    return operator, None


def _leg_for(operator, leg_id):
    """A leg this operator actually holds. Anything else is a 404, not a 403 —
    same posture as the chauffeur portal (don't confirm the leg exists)."""
    return get_object_or_404(
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "vehicle", "flight_information", "cruise_information",
        ),
        id=leg_id,
        driver=operator,
    )


def _decorate(legs):
    """Attach the copy payloads and the response state each card renders from."""
    for leg in legs:
        leg.job_fields = build_job_fields(leg)
        leg.job_text = build_job_text(leg)
        leg.needs_response = leg.operator_accepted_at is None and leg.status == "in-progress"
        leg.trip_kind = leg.get_trip_type()
        # Headline route reads as a route, not two Google address strings. The
        # full address is still one click away in the copy fields below it.
        leg.pickup_short = short_place(leg.pickup_location)
        leg.dropoff_short = short_place(leg.dropoff_location)
    return legs


def _base_queryset(operator):
    return (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "vehicle", "flight_information", "cruise_information",
        )
        .filter(driver=operator)
        .exclude(status="cancelled")
        .exclude(reservation__status="cancelled")
    )


@login_required(login_url="login")
def operator_board(request):
    """The operator's whole portal: response queue on top, then a day view.

    The queue spans dates on purpose. Everything else is scoped to one day
    because that's the unit an operator loads into their system and staffs.
    """
    operator = get_operator(request)
    if operator is None:
        # A chauffeur who lands here (stale link, shared bookmark) belongs in
        # their own portal, not on an error page.
        return redirect("schedule")

    today = timezone.localdate()
    selected_date = request.GET.get("date")
    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date else today
        )
    except ValueError:
        selected_date = today

    base = _base_queryset(operator)

    # ── Response queue: unanswered farm-outs, soonest first ──
    pending = list(
        base.filter(
            operator_accepted_at__isnull=True,
            status="in-progress",
            pickup_date__gte=today,
            pickup_date__lte=today + timedelta(days=QUEUE_HORIZON_DAYS),
        ).order_by("pickup_date", "pickup_time")
    )

    # ── The selected day ──
    day_legs = list(
        base.filter(pickup_date=selected_date).order_by("pickup_time")
    )

    _decorate(pending)
    _decorate(day_legs)

    # Jobs still missing a named chauffeur, so the operator can see at a glance
    # what they haven't staffed yet. Accepted-but-unstaffed is the gap that
    # bites at 4am, so it gets counted separately from the response queue.
    unstaffed = [
        leg for leg in day_legs
        if not leg.operator_driver_name and leg.status not in ("completed", "cancelled")
    ]

    return render(
        request,
        "drivers/operator_board.html",
        {
            "operator": operator,
            "pending": pending,
            "legs": day_legs,
            "unstaffed_count": len(unstaffed),
            "selected_date": selected_date,
            "is_today": selected_date == today,
            "prev_date": selected_date - timedelta(days=1),
            "next_date": selected_date + timedelta(days=1),
            "day_text": build_day_text(day_legs),
            "status_labels": OPERATOR_STATUS_LABELS,
            "decline_reasons": DECLINE_REASONS,
            "known_drivers": recent_operator_drivers(operator),
            "pending_count": len(pending),
        },
    )


def _pending_count(operator, today=None):
    """Unanswered farm-outs. Rendered as a badge in the nav on every page, so an
    operator can't sit on a job just because they were looking at Completed."""
    today = today or timezone.localdate()
    return _base_queryset(operator).filter(
        operator_accepted_at__isnull=True,
        status="in-progress",
        pickup_date__gte=today,
        pickup_date__lte=today + timedelta(days=QUEUE_HORIZON_DAYS),
    ).count()


@login_required(login_url="login")
def operator_upcoming(request):
    """Everything ahead, grouped by day.

    The board answers "what am I running today"; this answers "what have I got
    on" — the view an operator needs when staffing next week or loading a batch
    of jobs into their system. Each day gets its own copy-the-day button.
    """
    operator = get_operator(request)
    if operator is None:
        return redirect("schedule")

    today = timezone.localdate()
    legs = list(
        _base_queryset(operator)
        .filter(pickup_date__gte=today)
        .exclude(status="completed")
        .order_by("pickup_date", "pickup_time")
    )
    _decorate(legs)

    days, current = [], None
    for leg in legs:
        if current is None or current["date"] != leg.pickup_date:
            current = {"date": leg.pickup_date, "is_today": leg.pickup_date == today, "legs": []}
            days.append(current)
        current["legs"].append(leg)
    for day in days:
        day["text"] = build_day_text(day["legs"])

    return render(
        request,
        "drivers/operator_upcoming.html",
        {
            "operator": operator,
            "days": days,
            "total": len(legs),
            "pending_count": sum(1 for leg in legs if leg.needs_response),
            "status_labels": OPERATOR_STATUS_LABELS,
            "decline_reasons": DECLINE_REASONS,
            "known_drivers": recent_operator_drivers(operator),
        },
    )


@login_required(login_url="login")
def operator_completed(request):
    """Finished jobs, newest first — how an operator checks our record against
    theirs at invoicing time. Paginated: an established affiliate has thousands,
    and the chauffeur portal already learned that lesson the hard way (the
    unbounded history incident, 2026-07-18)."""
    operator = get_operator(request)
    if operator is None:
        return redirect("schedule")

    legs_qs = (
        _base_queryset(operator)
        .filter(status="completed")
        .order_by("-pickup_date", "-pickup_time")
    )
    page = Paginator(legs_qs, 25).get_page(request.GET.get("page"))
    _decorate(page.object_list)

    return render(
        request,
        "drivers/operator_completed.html",
        {
            "operator": operator,
            "legs": page,
            "read_only_page": True,
            "pending_count": _pending_count(operator),
            "status_labels": OPERATOR_STATUS_LABELS,
            "decline_reasons": DECLINE_REASONS,
            "known_drivers": recent_operator_drivers(operator),
        },
    )


def recent_operator_drivers(operator, days=120):
    """Distinct (name, phone) pairs this operator has used lately.

    A roster with no admin screen: the names they type become the suggestions
    they pick from next time, so the second job with the same chauffeur is one
    tap instead of re-typing a cell number. Bounded by time so a driver who left
    the company drops off the list on their own.
    """
    since = timezone.localdate() - timedelta(days=days)
    rows = (
        Leg.objects.filter(driver=operator, pickup_date__gte=since)
        .exclude(operator_driver_name="")
        .values_list("operator_driver_name", "operator_driver_phone")
        .order_by("-pickup_date")
    )
    seen, out = set(), []
    for name, phone in rows:
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name.strip(), "phone": (phone or "").strip()})
        if len(out) >= 25:
            break
    return out


@login_required(login_url="login")
@require_POST
def operator_accept(request, leg_id):
    """Operator takes the job. Mirrors views.accept_job plus the accept stamp."""
    operator, err = _operator_or_403(request)
    if err:
        return err
    leg = _leg_for(operator, leg_id)

    if leg.status in ("completed", "cancelled"):
        return JsonResponse(
            {"success": False, "error": "This job is already closed."}, status=400
        )

    leg.operator_accepted_at = timezone.now()
    # Accepting clears any earlier decline: same leg, same operator, new answer.
    leg.operator_declined_by = None
    leg.operator_declined_at = None
    leg.operator_decline_reason = ""
    fields = [
        "operator_accepted_at", "operator_declined_by",
        "operator_declined_at", "operator_decline_reason",
    ]
    if leg.status == "in-progress":
        leg.status = "confirmed"
        fields.append("status")
    leg.save(update_fields=fields)

    if "status" in fields:
        LegStatus.objects.create(
            leg=leg, status="confirmed", updated_by=request.user,
            timestamp=timezone.now(), notes="Accepted by operator",
        )

    return JsonResponse({"success": True, "new_status": leg.status})


@login_required(login_url="login")
@require_POST
def operator_decline(request, leg_id):
    """Operator gives the job back — it must land back on OUR board, loudly.

    Three things happen together, because a decline that only unassigns is a
    silent hole in the day: the leg is unassigned (Leg.save resets its status
    and clears the stale pay), a KEOI watch flag is raised so it shows up on the
    board's existing needs-attention surface, and dispatch gets a text.
    """
    operator, err = _operator_or_403(request)
    if err:
        return err
    leg = _leg_for(operator, leg_id)

    if leg.status in ("completed", "cancelled"):
        return JsonResponse(
            {"success": False, "error": "This job is already closed."}, status=400
        )

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    reason = (data.get("reason") or "").strip()[:255]
    if not reason:
        return JsonResponse(
            {"success": False, "error": "Tell dispatch why, so we can re-cover it."},
            status=400,
        )

    leg.operator_declined_by = operator
    leg.operator_declined_at = timezone.now()
    leg.operator_decline_reason = reason
    leg.operator_accepted_at = None
    leg.driver = None
    # _status_change_user attributes Leg.save's automatic unassign-reset in the
    # LegStatus history to the operator rather than leaving it unattributed.
    leg._status_change_user = request.user
    leg.save()

    _raise_decline_flag(leg, operator, reason, request.user)
    _notify_dispatch_of_decline(leg, operator, reason)

    return JsonResponse({"success": True})


def _raise_decline_flag(leg, operator, reason, actor):
    """Put the declined leg on the board's needs-attention surface (KEOI).

    Reuses the dispatcher watch-flag rather than inventing a second alert
    channel — it already renders on the board, opens in the modal, and
    auto-closes when the leg completes or cancels. One active flag per leg is a
    DB constraint, so an existing flag is left alone (it is already shouting).
    """
    try:
        if LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).exists():
            return
        LegKeoi.objects.create(
            leg=leg,
            category=LegKeoi.Category.DRIVER_CONFLICT,
            description=f"{operator} declined this farm-out: {reason}. Leg is unassigned — needs coverage.",
            operational_status=LegKeoi.OperationalStatus.NEEDS_ATTENTION,
            created_by=actor,
        )
    except Exception:
        # A decline must never fail because the flag couldn't be written; the
        # unassign already happened and the SMS still goes out.
        logger.exception("Could not raise KEOI for declined leg %s", leg.id)


def _notify_dispatch_of_decline(leg, operator, reason):
    """Text dispatch. Same Twilio path the time-off workflow uses.

    Two hard guards before anything reaches Twilio:
      * never under the test runner — the decline tests would otherwise send a
        real text to a real founder's phone on every run;
      * only to FARMOUT_NOTIFY_PHONES, which must be set deliberately. It does
        NOT fall back to the time-off list: turning this feature on is a
        decision, not something inherited from an unrelated setting.
    """
    if getattr(settings, "TESTING", False):
        return
    phones = getattr(settings, "FARMOUT_NOTIFY_PHONES", None) or []
    if not phones:
        logger.info(
            "Leg %s declined by %s — FARMOUT_NOTIFY_PHONES unset, no SMS sent "
            "(the leg is unassigned and KEOI-flagged on the board).",
            leg.id, operator,
        )
        return
    try:
        from drivers.timeoff_notifications import _send
        when = f"{leg.pickup_date:%b %d} {leg.pickup_time:%I:%M %p}".replace(" 0", " ")
        body = "\n".join([
            "FARM-OUT DECLINED",
            f"{operator} gave back {leg.pickup_location} → {leg.dropoff_location}",
            f"{when} — reason: {reason}",
            "Leg is unassigned and flagged on the board.",
        ])
        for phone in phones:
            _send(phone, body)
    except Exception:
        logger.exception("Could not text dispatch about declined leg %s", leg.id)


@login_required(login_url="login")
@require_POST
def operator_assign_driver(request, leg_id):
    """Record which of the operator's own chauffeurs is on the job.

    Always optional — an operator who never uses it keeps working exactly as
    before. Sending an empty name clears the assignment (they pulled the driver
    off), which is why blank is accepted rather than rejected.
    """
    operator, err = _operator_or_403(request)
    if err:
        return err
    leg = _leg_for(operator, leg_id)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    name = (data.get("name") or "").strip()[:120]
    phone = (data.get("phone") or "").strip()[:25]
    if not name:
        phone = ""  # no anonymous phone numbers hanging off a leg

    leg.operator_driver_name = name
    leg.operator_driver_phone = phone
    leg.save(update_fields=["operator_driver_name", "operator_driver_phone"])

    return JsonResponse({"success": True, "name": name, "phone": phone})
