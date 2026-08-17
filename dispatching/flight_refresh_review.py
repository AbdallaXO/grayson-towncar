"""
Post-refresh review helpers for the dispatcher's bulk flight refresh.

Pure functions (no view code) that turn the raw refresh results from
_run_bulk_flight_refresh into a structured summary the UI can render and
that auto-files flight_verify ops tasks for risky legs.
"""

from datetime import datetime, timedelta
import logging

from django.urls import reverse
from django.utils import timezone
from business.datefmt import strf

logger = logging.getLogger(__name__)


# ── Bucket / severity constants (kept simple — strings travel cleanly to JSON / JS) ──

STATUS_OK = "ok"
STATUS_MINOR = "minor_change"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_MANUAL = "manual_action"
STATUS_MISSING = "missing"

SEVERITY_SUCCESS = "success"
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_MANUAL = "manual"
SEVERITY_DANGER = "danger"

# Highest severity wins when multiple triggers fire for the same row.
_STATUS_RANK = {
    STATUS_MISSING: 4,
    STATUS_MANUAL: 3,
    STATUS_NEEDS_REVIEW: 2,
    STATUS_MINOR: 1,
    STATUS_OK: 0,
}

# Mismatch (in minutes) past which we escalate Needs Review → Manual Action.
LARGE_MISMATCH_MINUTES = 120


def best_arrival(flight):
    """
    Mirror of dispatching.views._best_flight_arrival_time so the snapshot/classify
    code can run in-thread without importing the view module.
    """
    if not flight:
        return None
    return (
        flight.actual_gate_arrival_local
        or flight.estimated_gate_arrival_local
        or flight.actual_arrival_local
        or flight.estimated_arrival_local
        or flight.scheduled_gate_arrival_local
        or flight.scheduled_arrival_local
    )


def snapshot_flight_state(flight):
    """
    Capture the fields we want to compare before/after a refresh.
    Returns None if the leg has no flight info.
    """
    if not flight:
        return None
    return {
        "flight_iata": flight.flight_iata or "",
        "status": flight.status or "",
        "scheduled_arrival_local": flight.scheduled_arrival_local,
        "scheduled_gate_arrival_local": flight.scheduled_gate_arrival_local,
        "estimated_arrival_local": flight.estimated_arrival_local,
        "estimated_gate_arrival_local": flight.estimated_gate_arrival_local,
        "actual_arrival_local": flight.actual_arrival_local,
        "actual_gate_arrival_local": flight.actual_gate_arrival_local,
        "best_arrival": best_arrival(flight),
    }


def _fmt_dt(dt):
    if not dt:
        return ""
    if hasattr(dt, "astimezone") and timezone.is_aware(dt):
        try:
            dt = timezone.localtime(dt)
        except Exception:
            pass
    try:
        return dt.strftime("%Y-%m-%d %I:%M %p").lstrip("0")
    except Exception:
        return str(dt)


def _arrival_date(flight):
    dt = best_arrival(flight)
    if not dt:
        return None
    if timezone.is_aware(dt):
        try:
            return timezone.localtime(dt).date()
        except Exception:
            return dt.date()
    return dt.date()


def _bump(current_status, candidate_status):
    """Return whichever status has higher severity rank."""
    if _STATUS_RANK.get(candidate_status, 0) > _STATUS_RANK.get(current_status, 0):
        return candidate_status
    return current_status


def _guest_name(leg):
    try:
        customer = leg.reservation.customer
        return f"{(customer.first_name or '').title()} {(customer.last_name or '').title()}".strip() or "Guest"
    except Exception:
        return "Guest"


def _driver_name(leg):
    driver = leg.driver
    if not driver:
        return ""
    try:
        return str(driver).strip()
    except Exception:
        return ""


def _reservation_url(leg):
    try:
        return reverse("reservation_details", args=[str(leg.reservation.uuid)])
    except Exception:
        return ""


def _edit_url(leg):
    try:
        return reverse("modify_reservation", args=[str(leg.reservation.uuid)])
    except Exception:
        return ""


def classify_refresh_row(leg, old_snapshot, refresh_result, threshold_minutes=30, minor_threshold_minutes=5):
    """
    Combine the per-leg refresh result + before/after flight snapshots
    into the structured dict the review modal renders.

    `refresh_result` is the dict produced by _refresh_single_flight:
    {"leg_id", "success": bool, "flight_data": {...}, "error": "...", "rate_limited": bool}
    `refresh_result` may be None if the worker skipped/failed before generating
    a result for this leg.
    """
    flight = leg.flight_information
    flight_iata = (flight.flight_iata if flight else "") or ""
    airline = (flight.airline_display_name if flight else "") or (flight.airline if flight else "") or ""
    flight_number = (flight.flight_number if flight else "") or ""

    new_best = best_arrival(flight)
    old_best = (old_snapshot or {}).get("best_arrival") if old_snapshot else None
    new_status = (flight.status if flight else "") or ""

    status = STATUS_OK
    severity = SEVERITY_SUCCESS
    issue_code = "ok"
    issue_label = "Flight refreshed — no action needed"
    recommended_action = ""

    # ── Missing bucket (refresh failed for any reason) ──
    if not refresh_result or not refresh_result.get("success"):
        err = (refresh_result or {}).get("error") or "Flight data could not be refreshed"
        status = STATUS_MISSING
        severity = SEVERITY_DANGER

        if not flight_iata and (not flight_number or not airline):
            issue_code = "missing_flight_number"
            issue_label = "Flight number or airline missing"
            recommended_action = "Add flight number and airline before sending confirmation"
        elif (refresh_result or {}).get("rate_limited"):
            issue_code = "rate_limited"
            issue_label = "AeroAPI rate-limited — try again in a minute"
            recommended_action = "Re-run Refresh Arrival Flights shortly"
        elif "not found" in err.lower() or "not_found" in err.lower():
            issue_code = "flight_not_found"
            issue_label = f"AeroAPI could not find {flight_iata or flight_number or 'this flight'}"
            recommended_action = "Verify the flight number / date is correct"
        elif "orlando" in err.lower():
            issue_code = "not_orlando"
            issue_label = "Flight does not arrive in Orlando"
            recommended_action = "Confirm the correct flight number for an MCO/SFB arrival"
        else:
            issue_code = "aeroapi_error"
            issue_label = err
            recommended_action = "Open the reservation and verify the flight manually"

    else:
        # Refresh succeeded. Inspect the new flight state.
        cancelled = "cancel" in new_status.lower()
        diverted = "divert" in new_status.lower()
        landed = "land" in new_status.lower()

        # Date shift — best arrival now falls on a different date than leg pickup
        new_arr_date = _arrival_date(flight)
        date_shifted = bool(new_arr_date and new_arr_date != leg.pickup_date)

        # Mismatch vs pickup — reuse the canonical Leg helper so logic stays in one place.
        mismatch = None
        try:
            mismatch = leg.get_flight_time_mismatch_display(threshold_minutes=threshold_minutes)
        except Exception:
            mismatch = None
        mismatch_minutes = (mismatch or {}).get("minutes") if mismatch else None
        mismatch_direction = (mismatch or {}).get("direction") if mismatch else None

        # Arrival changed from before-refresh snapshot: >= threshold is the
        # material Needs Review case, minor..threshold is the low-key Minor
        # Change bucket (sub-30-min moves dispatchers used to miss).
        arrival_changed_minutes = None
        arrival_changed_minor_minutes = None
        if old_best and new_best:
            try:
                delta = abs((new_best - old_best).total_seconds()) / 60.0
                if delta >= threshold_minutes:
                    arrival_changed_minutes = int(round(delta))
                elif delta >= minor_threshold_minutes:
                    arrival_changed_minor_minutes = int(round(delta))
            except Exception:
                pass

        if cancelled:
            status = _bump(status, STATUS_MANUAL)
            issue_code = "flight_cancelled"
            issue_label = f"Flight {flight_iata or flight_number} is cancelled"
            recommended_action = "Call guest — flight cancelled"
        elif diverted:
            status = _bump(status, STATUS_MANUAL)
            issue_code = "flight_diverted"
            issue_label = f"Flight {flight_iata or flight_number} is diverted"
            recommended_action = "Call guest — flight diverted"

        if date_shifted:
            status = _bump(status, STATUS_MANUAL)
            if issue_code == "ok":
                issue_code = "date_shifted"
                issue_label = (
                    f"Flight now arrives {strf(new_arr_date, '%a %b %-d')}"
                    f" (booked pickup {strf(leg.pickup_date, '%a %b %-d')})"
                )
                recommended_action = "Confirm new arrival date with guest before sending confirmation"

        if mismatch_minutes is not None:
            if mismatch_minutes >= LARGE_MISMATCH_MINUTES:
                status = _bump(status, STATUS_MANUAL)
                if issue_code == "ok":
                    issue_code = "large_delay"
                    issue_label = (
                        f"Pickup off by {mismatch_minutes} min — {mismatch['label'].lower()}"
                    )
                    recommended_action = "Call guest and update pickup time"
            else:
                status = _bump(status, STATUS_NEEDS_REVIEW)
                if issue_code == "ok":
                    issue_code = "pickup_flight_mismatch"
                    issue_label = (
                        f"Pickup differs from flight by {mismatch_minutes} min ({mismatch['direction']})"
                    )
                    recommended_action = "Review pickup time before sending confirmation"

        if arrival_changed_minutes is not None and status == STATUS_OK:
            status = _bump(status, STATUS_NEEDS_REVIEW)
            issue_code = "arrival_changed"
            issue_label = f"Flight arrival moved by {arrival_changed_minutes} min since last check"
            recommended_action = "Confirm pickup still lines up with new arrival"

        if landed and status == STATUS_OK and old_best and new_best:
            # Landed flights with a meaningful actual-vs-scheduled swing
            try:
                swing = abs((new_best - old_best).total_seconds()) / 60.0
                if swing >= LARGE_MISMATCH_MINUTES:
                    status = STATUS_MANUAL
                    issue_code = "large_delay"
                    issue_label = f"Landed {int(swing)} min off schedule"
                    recommended_action = "Confirm dispatcher is on top of the actual landing time"
            except Exception:
                pass

        # ── Minor Change bucket — only when nothing above flagged the row ──
        if arrival_changed_minor_minutes is not None and status == STATUS_OK:
            status = _bump(status, STATUS_MINOR)
            issue_code = "arrival_changed_minor"
            issue_label = f"Flight arrival moved {arrival_changed_minor_minutes} min since last check"
            recommended_action = "Confirm pickup + driver turnaround still line up"

        if status == STATUS_OK:
            # Small pickup-vs-flight drift (minor..threshold min) — the canonical
            # helper returns None below its threshold, so re-ask with the minor one.
            try:
                minor_mismatch = leg.get_flight_time_mismatch_display(
                    threshold_minutes=minor_threshold_minutes
                )
            except Exception:
                minor_mismatch = None
            minor_minutes = (minor_mismatch or {}).get("minutes") if minor_mismatch else None
            if minor_minutes is not None and minor_minutes < threshold_minutes:
                status = _bump(status, STATUS_MINOR)
                issue_code = "pickup_flight_minor_mismatch"
                issue_label = (
                    f"Pickup differs from flight by {minor_minutes} min ({minor_mismatch['direction']})"
                )
                recommended_action = "Confirm pickup + driver turnaround still line up"

        # Pick a severity from the chosen status
        severity = {
            STATUS_OK: SEVERITY_SUCCESS,
            STATUS_MINOR: SEVERITY_INFO,
            STATUS_NEEDS_REVIEW: SEVERITY_WARNING,
            STATUS_MANUAL: SEVERITY_MANUAL,
            STATUS_MISSING: SEVERITY_DANGER,
        }[status]

    # Set severity for Missing too (in case we fell through without setting it)
    if status == STATUS_MISSING:
        severity = SEVERITY_DANGER

    # Recompute mismatch payload for the row regardless of bucket. Uses the
    # MINOR threshold so minor rows carry their minutes too (compare badge +
    # Match-All preview) instead of rendering as "matches flight".
    try:
        mismatch_for_row = leg.get_flight_time_mismatch_display(threshold_minutes=minor_threshold_minutes)
    except Exception:
        mismatch_for_row = None

    # Surface the verify-email "last sent" state so the modal can render
    # "Email sent X ago" instead of re-offering the verify button when the
    # guest hasn't acted yet.
    verify_sent_at = getattr(leg, "flight_verification_email_sent_at", None)
    if verify_sent_at is not None:
        hours_since_verify = max(
            0.0, (timezone.now() - verify_sent_at).total_seconds() / 3600
        )
        verify_sent_at_iso = verify_sent_at.isoformat()
    else:
        hours_since_verify = None
        verify_sent_at_iso = None

    return {
        "leg_id": leg.id,
        "reservation_id": leg.reservation_id,
        "reservation_uuid": str(getattr(leg.reservation, "uuid", "") or ""),
        "guest_name": _guest_name(leg),
        "status": status,
        "severity": severity,
        "issue_code": issue_code,
        "issue_label": issue_label,
        "recommended_action": recommended_action,
        "old_arrival_time": _fmt_dt(old_best),
        "new_arrival_time": _fmt_dt(new_best),
        "pickup_time": leg.pickup_time.strftime("%I:%M %p").lstrip("0") if leg.pickup_time else "",
        "pickup_date": leg.pickup_date.strftime("%Y-%m-%d") if leg.pickup_date else "",
        "mismatch_minutes": (mismatch_for_row or {}).get("minutes") if mismatch_for_row else None,
        "mismatch_direction": (mismatch_for_row or {}).get("direction") if mismatch_for_row else None,
        "flight_number": flight_number,
        "airline": airline,
        "flight_iata": flight_iata,
        "flight_status": new_status,
        "pickup_location": leg.pickup_location or "",
        "dropoff_location": leg.dropoff_location or "",
        "assigned_driver": _driver_name(leg),
        "reservation_url": _reservation_url(leg),
        "edit_url": _edit_url(leg),
        "verify_email_sent_at": verify_sent_at_iso,
        "verify_email_hours_since": hours_since_verify,
    }


def build_review_summary(rows, minor_threshold_minutes=5, threshold_minutes=30):
    """
    Tally totals + sort rows so the UI can render directly from the response.
    Sort: missing → manual_action → needs_review → minor_change → ok
    (within each, by mismatch desc).
    """
    totals = {
        "total": len(rows),
        STATUS_OK: 0,
        STATUS_MINOR: 0,
        STATUS_NEEDS_REVIEW: 0,
        STATUS_MANUAL: 0,
        STATUS_MISSING: 0,
    }
    for r in rows:
        totals[r["status"]] = totals.get(r["status"], 0) + 1

    def sort_key(r):
        return (
            -_STATUS_RANK.get(r["status"], 0),
            -(r.get("mismatch_minutes") or 0),
            r["leg_id"],
        )

    sorted_rows = sorted(rows, key=sort_key)
    problem_rows = [r for r in sorted_rows if r["status"] != STATUS_OK]

    return {
        "totals": totals,
        "rows": sorted_rows,
        "problem_count": len(problem_rows),
        # Bucket boundaries, so the JS never hard-codes them again.
        "thresholds": {
            "minor": minor_threshold_minutes,
            "review": threshold_minutes,
            "manual": LARGE_MISMATCH_MINUTES,
        },
    }


def auto_create_flight_verify_tasks(rows, *, created_by=None):
    """
    For every row classified needs_review / manual_action / missing, ensure an
    open FLIGHT_VERIFICATION ops task exists. Dedup + cooldown handled by
    ops.services.create_task. Skips rows where _refresh_single_flight has
    already created the task (issue_code='flight_not_found' / 'not_orlando')
    to avoid double-creates.
    """
    from ops.models import OperationalTask
    from ops.services import create_task
    from reservations.models import Leg

    SKIP_CODES = {"flight_not_found", "not_orlando"}
    created = 0

    for row in rows:
        # Minor changes are dispatcher FYI only — no task, like OK rows.
        if row["status"] in (STATUS_OK, STATUS_MINOR):
            continue
        if row["issue_code"] in SKIP_CODES:
            continue

        try:
            leg = Leg.objects.select_related("reservation").get(id=row["leg_id"])
        except Leg.DoesNotExist:
            continue

        priority = (
            OperationalTask.Priority.HIGH
            if row["status"] in (STATUS_MANUAL, STATUS_MISSING)
            else OperationalTask.Priority.MEDIUM
        )

        title = f"Flight review: {row['guest_name']} — {row['issue_label']}"[:200]
        description = (
            f"{row['recommended_action']}\n\n"
            f"Booked pickup: {row['pickup_date']} {row['pickup_time']}\n"
            f"Flight: {row['flight_iata'] or row['flight_number'] or '—'} ({row['airline'] or '—'})\n"
            f"Arrival: {row['new_arrival_time'] or '—'}"
        )

        try:
            task = create_task(
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                title=title,
                description=description,
                priority=priority,
                due_at=timezone.now() + timedelta(hours=4),
                leg=leg,
                reservation=leg.reservation,
                created_by=created_by,
                metadata={
                    "source": "flight_refresh_review",
                    "issue_code": row["issue_code"],
                    "mismatch_minutes": row.get("mismatch_minutes"),
                    "mismatch_direction": row.get("mismatch_direction"),
                    "old_arrival_time": row.get("old_arrival_time"),
                    "new_arrival_time": row.get("new_arrival_time"),
                    "flight_iata": row.get("flight_iata"),
                },
            )
            if task is not None:
                created += 1
        except Exception as e:
            logger.error(f"auto_create_flight_verify_tasks: leg {row['leg_id']} failed — {e}")

    return created
