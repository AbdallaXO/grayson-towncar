"""
The Team page — one screen for whoever leads the floor.

Founder ask 2026-10-02: the team lead needs "a small system to manage/lead and
view every staff — what they're doing today". This assembles it from what the
app already records; nothing here writes anything.

  * what needs a decision: refund requests waiting (for whoever can approve
    them), tasks nobody has picked up, today's open and close checklists;
  * one simple line per dispatcher who is on today (founder ask, same day:
    "how many reservations they booked today, how many tasks they finished,
    in plain terms… who's working, who's idling"): right now — busy, idle,
    on a break, not in, done — then booked today, tasks done, and the last
    thing they did in plain words. Opening the line shows where they are
    against the hours they were down for and what is on their plate;
  * everyone else, in one line, as off today.

"Idle" means clocked in with nothing done in the app for IDLE_AFTER. It reads
the activity log (pages, tasks, flights) and the change log (assigned, booked,
edited) together; page visits alone are logged once per page per half hour.

Look only, like the staffing board for a lead: no revenue, no pay, no hour
totals across days. Lateness comes from scheduling.schedule_vs_actual — the
same call the admin Time Clock hub makes — so "late" means one thing app-wide.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

from django.db.models import Count, Max, Min
from django.urls import Resolver404, resolve
from django.utils import timezone

from business.datefmt import time12
from drivers.availability import fmt_time_long

from . import scheduling
from .models import (
    OperationalTask,
    ShiftChecklist,
    ShiftException,
    StaffActivity,
    StaffOnCall,
    TimeClockShift,
)
from .staff import office_staff_qs

# A page someone opened, said the way the floor names its screens. Ordered
# roughly by how often each is visited (September 2026 activity log).
_PAGE_WORDS = {
    "reservation_details": "Opened a reservation",
    "schedule_board": "Opened the board",
    "dashboard": "Opened the dashboard",
    "reservations_list": "Opened reservations",
    "capacity_planner": "Opened the planner",
    "task_detail": "Opened a task",
    "task_queue": "Opened tasks",
    "modify_reservation": "Edited a reservation",
    "dispatcher_payment_portal": "Taking a payment",
    "timeclock": "Opened their clock",
    "my_coverage": "Opened their schedule",
    "quote_calculator": "Made a quote",
    "confirmations": "Opened confirmations",
    "staffing_board": "Opened the staffing board",
    "leg_history": "Looked at a trip's history",
    "timeclock_overview": "Opened who's on now",
    "refund_management": "Opened refunds",
    "shift_open": "Opened the open checklist",
    "shift_close": "Opened the close checklist",
    "shift_lead": "Opened how shifts ran",
    "swap_tester": "Opened the swap tester",
    "dispatcher_timeoff_requests": "Opened driver time off",
    "inhouse_schedule": "Opened driver schedules",
    "driver_schedules_dashboard": "Opened driver schedules",
    "drivers_extend": "Opened drivers",
    "legs_list": "Opened legs",
    "team_today": "Opened the team page",
}
_PAGE_PREFIX_WORDS = (
    ("dispatcher_booking_", "Working on a new booking"),
    ("fleet_", "Opened fleet"),
    ("timeclock_", "Opened the time clock"),
)

# Who needs a look first. Lower sorts higher.
_ORDER = {"no_show": 0, "not_in": 1, "unclocked": 2, "working": 3, "on_break": 4,
          "later": 5, "scheduled": 6, "done": 7}

TASKS_SHOWN = 3

# Clocked in with nothing done in the app for this long reads "Idle". Page
# visits are only logged once per page per half hour, so anything shorter would
# call a dispatcher working one screen idle.
IDLE_AFTER = timedelta(minutes=30)

# The last thing someone did, in plain words — from the change log (who
# assigned, booked, edited) and the activity log (tasks, flights, pages).
_CHANGE_WORDS = {
    ("driver_assigned", ""): "Assigned a driver",
    ("driver_unassigned", ""): "Took a driver off a trip",
    ("status_changed", ""): "Updated a trip's status",
    ("payment_processed", ""): "Took a payment",
    ("created", "Reservation"): "Booked a reservation",
    ("created", "Leg"): "Added a trip",
    ("updated", "Reservation"): "Edited a reservation",
    ("updated", "Leg"): "Edited a trip",
    ("deleted", "Reservation"): "Deleted a reservation",
    ("deleted", "Leg"): "Removed a trip",
}
_ACTIVITY_WORDS = {
    StaffActivity.ActionType.TASK_COMPLETED: "Finished a task",
    StaffActivity.ActionType.TASK_CLAIMED: "Picked up a task",
    StaffActivity.ActionType.TASK_SNOOZED: "Snoozed a task",
    StaffActivity.ActionType.TASK_ASSIGNED: "Handed a task on",
    StaffActivity.ActionType.TASK_CREATED: "Made a task",
    StaffActivity.ActionType.COMM_LOGGED: "Logged a guest message",
    StaffActivity.ActionType.FLIGHT_MATCHED: "Matched a flight time",
    StaffActivity.ActionType.AFTERHOURS_SETTLED: "Settled an after-hours fee",
    StaffActivity.ActionType.SHIFT_OPENED: "Opened the shift checklist",
    StaffActivity.ActionType.SHIFT_ROW_CONFIRMED: "Worked the shift checklist",
    StaffActivity.ActionType.SHIFT_EXCEPTION_RAISED: "Wrote a shift note",
    StaffActivity.ActionType.SHIFT_COMPLETED: "Finished the shift checklist",
    StaffActivity.ActionType.SHIFT_REOPENED: "Reopened the shift checklist",
}


def _change_words(action, model_name):
    return _CHANGE_WORDS.get((action, model_name)) or _CHANGE_WORDS.get((action, ""), "Made a change")


def _activity_words(action_type, path):
    if action_type == StaffActivity.ActionType.PAGE_VIEW:
        return _page_words(path) if path else "Opened the app"
    return _ACTIVITY_WORDS.get(action_type, "Used the app")


def _dur(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _right_now(row, *, open_shift, last_action_at, now):
    """The one-glance answer for the activity line: (words, tone)."""
    state = row["state"]
    if state == "on_break":
        return f"On a break · {_dur(now - open_shift.open_break.break_start_at)}", "info"
    if state == "working":
        anchor = max(t for t in (last_action_at, open_shift.clock_in_at) if t)
        if now - anchor >= IDLE_AFTER:
            return f"Idle · {_dur(now - anchor)}", "warn"
        return "Busy", "ok"
    if state == "scheduled" and last_action_at and now - last_action_at < IDLE_AFTER:
        return "Busy", "ok"  # doesn't use the clock, but plainly at it
    due = f" · due {row['due']}" if row.get("due") else ""
    return {
        "no_show": (f"Not in{due}", "bad"),
        "not_in": (f"Not in yet{due}", "warn"),
        "unclocked": ("In the app, not clocked in", "warn"),
        "done": ("Done for the day", "quiet"),
    }.get(state, (row["label"], "quiet"))


def _t(dt):
    return time12(timezone.localtime(dt)) if dt else ""


def _page_words(path):
    try:
        name = resolve(path).url_name or ""
    except Resolver404:
        return "Opened the app"
    if name in _PAGE_WORDS:
        return _PAGE_WORDS[name]
    for prefix, words in _PAGE_PREFIX_WORDS:
        if name.startswith(prefix):
            return words
    return "Opened the app"


def _day_bounds(day):
    start = timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())
    return start, start + timedelta(days=1)


def _role_label(role):
    return {"opener": "Opens", "closer": "Closes", "both": "Opens and closes", "mid": "Mid-day"}.get(role or "", "")


def _person(u, *, vs, sched, open_shift, shifts_today, activity, oncall_tonight, now):
    """One dispatcher's line for today, or None when they are simply off."""
    status = vs["status"]
    start = sched["start_time"]
    starts_later = bool(start and timezone.localtime(now).time() < start)
    note = ""
    since = None

    if open_shift is not None:
        brk = open_shift.open_break
        if brk is not None:
            state, tone, since = "on_break", "info", brk.break_start_at
            label = f"On a break since {_t(since)}"
        else:
            state, tone, since = "working", "ok", open_shift.clock_in_at
            label = f"Working since {_t(since)}"
        if status == "late_start" and sched["start_time"] and shifts_today:
            first_in = min(s.clock_in_at for s in shifts_today)
            note = f"In late — clocked in {_t(first_in)}, was due {fmt_time_long(sched['start_time'])}"
    elif shifts_today:
        last_out = max((s.clock_out_at for s in shifts_today if s.clock_out_at), default=None)
        state, tone, label = "done", "quiet", f"Clocked out {_t(last_out)}"
        if status == "left_early" and sched["end_time"]:
            note = f"Left early — was down until {fmt_time_long(sched['end_time'])}"
    elif status == "absent":
        due = fmt_time_long(sched["start_time"]) if sched["start_time"] else "earlier"
        if vs["detail"].get("no_show"):
            state, tone, label = "no_show", "bad", f"Hasn't clocked in — was due {due}"
        else:
            state, tone, label = "not_in", "warn", f"Not in yet — due {due}"
    elif status == "upcoming" or (status == "untracked" and starts_later):
        state, tone = "later", "quiet"
        label = f"Starts {fmt_time_long(start)}" if start else "On later today"
    elif status == "untracked":
        # Down for today but has never used the clock, so in or out can't be told.
        state, tone = "scheduled", "quiet"
        label = (f"Down from {fmt_time_long(start)} — doesn't use the clock yet" if start
                 else "Down for today — doesn't use the clock yet")
    elif activity:
        state, tone, label = "unclocked", "warn", "Working in the app, not clocked in"
    elif oncall_tonight:
        state, tone, label = "scheduled", "quiet", "Off today — on call tonight"
    else:
        return None

    scheduled = vs["scheduled_label"] if sched["is_working"] or vs.get("is_split") else ""
    return {
        "user": u,
        "name": u.get_full_name() or u.username,
        "state": state,
        "tone": tone,
        "label": label,
        "note": note,
        "since_ms": int(since.timestamp() * 1000) if since else None,
        "scheduled": scheduled if scheduled not in ("—", "No schedule") else "",
        "due": fmt_time_long(start) if start else "",
        "role": _role_label(sched.get("role", "")),
        "location": {"office": "Office", "remote": "Working from home"}.get(sched.get("location", ""), ""),
        "oncall_tonight": oncall_tonight,
    }


def build(now=None, *, include_refunds=False):
    """Everything the Team page shows, as plain data for the template."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    day_start, day_end = _day_bounds(today)

    roster = list(office_staff_qs().prefetch_related("schedule_overrides", "weekly_schedule_rows", "extra_shifts"))
    uids = [u.id for u in roster]

    # ── Clock ──
    open_by_user = {
        s.user_id: s
        for s in TimeClockShift.objects.filter(clock_out_at__isnull=True, user_id__in=uids).prefetch_related("breaks")
    }
    shifts_today = defaultdict(list)
    for s in TimeClockShift.objects.filter(
        user_id__in=uids, clock_in_at__gte=day_start, clock_in_at__lt=day_end,
    ).prefetch_related("breaks"):
        shifts_today[s.user_id].append(s)
    # A staffer who has never used the clock is "not using it yet", never "absent".
    tracking_since = {
        row["user_id"]: timezone.localtime(row["first"]).date()
        for row in TimeClockShift.objects.filter(user_id__in=uids).values("user_id").annotate(first=Min("clock_in_at"))
    }
    oncall_tonight = set(
        StaffOnCall.objects.filter(date=today + timedelta(days=1), user_id__in=uids).values_list("user_id", flat=True)
    )

    # ── What is on each plate, and what each has got through today ──
    open_task_rows = (
        OperationalTask.objects.filter(status__in=OperationalTask.OPEN_STATUSES, assigned_to_id__in=uids)
        .order_by("priority", "due_at")
        .values("id", "assigned_to_id", "title", "due_at")
    )
    tasks_by_user = defaultdict(list)
    for row in open_task_rows:
        tasks_by_user[row["assigned_to_id"]].append(row)

    def _counts(qs, key):
        return {row[key]: row["c"] for row in qs.values(key).annotate(c=Count("id"))}

    done_today = _counts(
        OperationalTask.objects.filter(
            resolved_by_id__in=uids, status=OperationalTask.Status.COMPLETED,
            resolved_at__gte=day_start, resolved_at__lt=day_end,
        ),
        "resolved_by_id",
    )
    from reservations.models import Reservation

    booked_today = _counts(
        Reservation.objects.filter(created_by_id__in=uids, created_at__gte=day_start, created_at__lt=day_end),
        "created_by_id",
    )
    notes_owned = _counts(ShiftException.objects.filter(resolved_at__isnull=True, owner_id__in=uids), "owner_id")

    logged = defaultdict(lambda: defaultdict(int))
    for row in (
        StaffActivity.objects.filter(
            user_id__in=uids, created_at__gte=day_start, created_at__lt=day_end,
            action_type__in=[StaffActivity.ActionType.COMM_LOGGED, StaffActivity.ActionType.FLIGHT_MATCHED],
        ).values("user_id", "action_type").annotate(c=Count("id"))
    ):
        logged[row["user_id"]][row["action_type"]] = row["c"]

    # The last thing each person did today: the latest of the activity log
    # (pages, tasks, flights) and the change log (assigned, booked, edited).
    last_seen = {}
    for row in (
        StaffActivity.objects.filter(user_id__in=uids, created_at__gte=day_start, created_at__lt=day_end)
        .order_by("user_id", "-created_at")
        .values("user_id", "path", "action_type", "created_at")
    ):
        last_seen.setdefault(row["user_id"], row)

    from reservations.models import AuditLog

    last_change = {}
    for row in (
        AuditLog.objects.filter(user_id__in=uids, timestamp__gte=day_start, timestamp__lt=day_end)
        .values("user_id").annotate(last=Max("timestamp"))
    ):
        hit = (
            AuditLog.objects.filter(user_id=row["user_id"], timestamp=row["last"])
            .order_by("-id").values("action", "model_name", "timestamp").first()
        )
        if hit:
            last_change[row["user_id"]] = hit

    # ── One line per person ──
    people, off = [], []
    for u in roster:
        sched = scheduling.resolve_staff_schedule(u, today)
        vs = scheduling.schedule_vs_actual(
            u, today, shifts=shifts_today.get(u.id, []), now=now, tracking_since=tracking_since.get(u.id),
        )
        seen = last_seen.get(u.id)
        change = last_change.get(u.id)
        last_at, last_did = None, ""
        if seen:
            last_at, last_did = seen["created_at"], _activity_words(seen["action_type"], seen["path"])
        if change and (last_at is None or change["timestamp"] >= last_at):
            last_at, last_did = change["timestamp"], _change_words(change["action"], change["model_name"])
        did = {
            "tasks_done": done_today.get(u.id, 0),
            "bookings": booked_today.get(u.id, 0),
            "messages": logged[u.id][StaffActivity.ActionType.COMM_LOGGED],
            "flights": logged[u.id][StaffActivity.ActionType.FLIGHT_MATCHED],
        }
        row = _person(
            u, vs=vs, sched=sched,
            open_shift=open_by_user.get(u.id),
            shifts_today=shifts_today.get(u.id, []),
            # An admin is on the roster (a founder who works a dispatch shift is
            # a real row) but never clocks in for the rest of the day's work, so
            # being busy in the app only counts for the dispatchers.
            activity=(last_at is not None or any(did.values())) and not u.is_superuser,
            oncall_tonight=u.id in oncall_tonight,
            now=now,
        )
        if row is None:
            if not u.is_superuser:
                # Booked off says so; the reason stays on the staffing board.
                off.append({
                    "name": u.get_full_name() or u.username,
                    "why": "time off" if sched.get("time_off") else "",
                })
            continue

        tasks = tasks_by_user.get(u.id, [])
        row.update({
            "did": did,
            "did_any": any(did.values()),
            "tasks_open": len(tasks),
            "tasks_overdue": sum(1 for t in tasks if t["due_at"] and t["due_at"] < now),
            "tasks_shown": tasks[:TASKS_SHOWN],
            "tasks_more": max(0, len(tasks) - TASKS_SHOWN),
            "notes_owned": notes_owned.get(u.id, 0),
            "last_did": last_did,
            "last_did_at": _t(last_at),
        })
        row["now_words"], row["now_tone"] = _right_now(
            row, open_shift=open_by_user.get(u.id), last_action_at=last_at, now=now,
        )
        people.append(row)

    people.sort(key=lambda r: (_ORDER.get(r["state"], 9), r["name"].lower()))

    # ── What needs a decision ──
    open_tasks = OperationalTask.objects.filter(status__in=OperationalTask.OPEN_STATUSES)
    unclaimed = open_tasks.filter(assigned_to__isnull=True)

    checklists = {c.kind: c for c in ShiftChecklist.objects.filter(date=today).select_related("opened_by", "completed_by")}

    def _checklist(kind, title):
        c = checklists.get(kind)
        if c is None or c.opened_at is None:
            text, tone = "Not started", "quiet"
        elif c.completed_at:
            who = c.completed_by.get_full_name() or c.completed_by.username if c.completed_by else "someone"
            text, tone = f"Done by {who} at {_t(c.completed_at)}", "ok"
        else:
            who = c.opened_by.get_full_name() or c.opened_by.username if c.opened_by else "someone"
            text, tone = f"Started by {who}, not finished", "warn"
        return {"title": title, "text": text, "tone": tone}

    out = {
        "today": today,
        "now": now,
        "people": people,
        "off": off,
        "team_booked": sum(p["did"]["bookings"] for p in people),
        "team_tasks_done": sum(p["did"]["tasks_done"] for p in people),
        "busy_count": sum(1 for p in people if p["now_words"] == "Busy"),
        "idle_count": sum(1 for p in people if p["now_words"].startswith("Idle")),
        "idle_after_min": int(IDLE_AFTER.total_seconds() // 60),
        "on_clock_count": len(open_by_user),
        "unclaimed_count": unclaimed.count(),
        "unclaimed_overdue": unclaimed.filter(due_at__lt=now).count(),
        "open_notes": ShiftException.objects.filter(resolved_at__isnull=True).count(),
        "checklist_open": _checklist(ShiftChecklist.Kind.OPEN, "Open"),
        "checklist_close": _checklist(ShiftChecklist.Kind.CLOSE, "Close"),
        "include_refunds": include_refunds,
    }

    if include_refunds:
        from reservations.models import RefundRequest

        waiting = RefundRequest.objects.filter(status="requested")
        out["refund_count"] = waiting.count()
        out["refunds"] = list(
            waiting.select_related("reservation__customer", "requested_by").order_by("requested_at")[:8]
        )
    return out
