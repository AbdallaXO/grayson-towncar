"""
One readable timeline for a leg, merged from every trail that records a change.

A leg's history lives in four places, and until now the history page read only
the first of them:

  1. HistoricalLeg (django-simple-history)  — row snapshots, diffed pairwise
  2. AuditLog                               — durable per-field rows, real clock
  3. StaffActivity (FLIGHT_MATCHED)         — dispatcher matched a pickup
  4. LegStatus                              — driver-side status taps

Reading only (1) is how a pickup move made at 9:30 PM by a dispatcher surfaced
six hours later under the name of the driver who happened to tap "Accept"
next: writes that went through queryset.update() left no snapshot, so
simple_history's consecutive-snapshot diff folded them into the following
save. The write path no longer does that (see dispatching/pickup_moves.py),
but every leg touched before the fix still carries those bundled rows — so
this module also *un-bundles* them, using the change's own stamped timestamp
to put it back at the hour it really happened.

Output is a list of plain dicts, newest first, ready for the template.
"""

import logging
from datetime import timedelta

from django.utils import timezone
from business.datefmt import strf

logger = logging.getLogger(__name__)

# Two writes describing the same real-world action never land more than this
# far apart. Used to fold the four sources together without double-reporting.
DEDUPE_WINDOW = timedelta(seconds=120)

# A pickup move whose stamped "changed at" predates its snapshot by more than
# this was recorded late — it belongs to an earlier moment and, critically, to
# someone other than whoever's name is on the snapshot.
LATE_RECORD_THRESHOLD = timedelta(minutes=3)

# Bookkeeping columns. They carry no information a dispatcher can act on, and
# listing them buries the two or three changes that matter.
NOISE_FIELDS = {
    "updated_at", "created_at", "last_modified_at", "modified_by",
    "pickup_change_ack_at", "history_change_reason",
    "_original_pickup_time", "_original_pickup_date",
}

# Fields the system fills in as a CONSEQUENCE of another change. Assigning a
# driver writes seven of them in one go — assigned_at, assigned_by, base pay,
# gratuity, extra, pay amount, profit estimate — none of which anyone decided.
# Listing them buries the one thing that happened ("Iris assigned David"), so
# they are folded away and reachable behind the per-event "show all fields"
# toggle rather than deleted.
DERIVED_FIELDS = {
    "driver_assigned_at", "driver_assigned_by", "status_changed_at",
    "profit_estimate", "revenue_share", "total_driver_payments",
    "pickup_time_changed_at", "pickup_time_was", "pickup_date_was",
    "pickup_change_ack_at",
}

PICKUP_FIELDS = {"pickup_time", "pickup_date"}
DRIVER_FIELDS = {"driver"}

# Pay trails a driver assignment automatically, so next to "assigned to David"
# it is noise. Edited on its own it is the entire point of the change — so it
# is suppressed only when a driver change is present in the same row.
PAY_FIELDS = {
    "driver_base_pay", "driver_gratuity", "driver_additional",
    "driver_pay_amount",
}
MONEY_FIELDS = {"afterhours_fee", "stop_fee", "leg_base_price"}

FIELD_LABELS = {
    "pickup_time": "Pickup time",
    "pickup_date": "Pickup date",
    "status": "Status",
    "driver": "Driver",
    "pickup_location": "Pickup location",
    "dropoff_location": "Drop-off location",
    "afterhours_fee": "After-hours fee",
    "driver_notes": "Driver notes",
    "confirmation_sms_sent_at": "Confirmation text sent",
    "flight_information": "Flight",
    "driver_base_pay": "Driver base pay",
    "driver_gratuity": "Driver gratuity",
    "driver_additional": "Driver extra",
    "driver_pay_amount": "Driver pay",
    "profit_estimate": "Profit estimate",
    "revenue_share": "Revenue share",
}


KIND_ICONS = {
    "pickup_moved": "bi-clock-history",
    "flight_match": "bi-airplane",
    "driver_assigned": "bi-person-check",
    "driver_unassigned": "bi-person-dash",
    "driver_changed": "bi-arrow-left-right",
    "status": "bi-flag",
    "status_run": "bi-check2-circle",
    "pay_changed": "bi-cash-coin",
    "created": "bi-plus-circle",
    "field_change": "bi-pencil",
    "audit": "bi-journal-text",
}

FLAG_LABELS = {
    # Spelled out rather than abbreviated: this badge is the difference between
    # "the driver retimed the trip" and "the trip was retimed hours earlier by
    # someone else and this snapshot just happened to notice".
    "late_recorded": "recorded late — happened before this entry",
    "day_moved": "calendar day changed",
    "actor_recovered": "name recovered from audit log",
}

ACTOR_KIND_LABELS = {
    "staff": "dispatcher",
    "driver": "driver",
    "guest": "guest",
    "system": "automatic",
}


def _label(field):
    return FIELD_LABELS.get(field, field.replace("_", " ").capitalize())


def _actor_name(user):
    if not user:
        return None
    return user.get_full_name() or user.username


def _fmt_time(t):
    return strf(t, "%-I:%M %p") if t else "—"


def _fmt_date(d):
    return strf(d, "%a %b %-d") if d else "—"


def _event(at, kind, title, actor=None, actor_kind="system", severity="info",
           details=None, source="history", note=None, flags=None):
    return {
        "at": at,
        "kind": kind,
        "title": title,
        "actor": actor,
        "actor_kind": actor_kind,
        "severity": severity,          # critical | warn | info | quiet
        "details": details or [],      # list of {"label", "from", "to"} or {"text"}
        "hidden_details": [],          # derived columns, folded behind a toggle
        "status_value": None,          # raw status, when this event is one
        "source": source,              # history | audit | activity | status
        "note": note,                  # why it happened, when known
        "flags": flags or [],          # ["late_recorded", "day_moved", ...]
    }


def _empty_fk(value):
    """
    True for simple_history's placeholder for a null foreign key.

    Diffing with foreign_keys_are_objs=True renders an absent FK as the string
    "Deleted driver (pk=None)" rather than None. Taken literally it turns every
    first assignment into a bogus "Driver changed: Deleted driver → David", and
    it is the same string that made the raw history page so hard to read.
    """
    return value is None or "pk=None" in str(value)


def _clean(value):
    """Display value, with the null-FK placeholder blanked out."""
    return None if _empty_fk(value) else value


def _detail(label, old, new):
    return {"label": label, "from": _clean(old), "to": _clean(new)}


def _text(text):
    return {"text": text}


# ─────────────────────────── source 1: snapshots ───────────────────────────

def _history_events(leg):
    """
    Turn consecutive HistoricalLeg snapshots into events.

    Splits a snapshot into more than one event where the row bundles unrelated
    work — most importantly a pickup move that was stamped earlier than the
    snapshot that reported it. Those get re-dated and de-attributed rather than
    left sitting under the wrong person's name.
    """
    from simple_history.utils import get_history_manager_for_model
    from reservations.models import Leg

    manager = get_history_manager_for_model(Leg)
    pk_attr = leg._meta.pk.attname
    records = list(
        manager.filter(**{pk_attr: getattr(leg, pk_attr)})
        .select_related("history_user")
        .order_by("-history_date")
    )

    events = []
    for index, record in enumerate(records):
        actor = _actor_name(record.history_user)
        actor_kind = "staff" if record.history_user else "system"
        reason = (getattr(record, "history_change_reason", "") or "").strip()

        if index + 1 >= len(records):
            events.append(_event(
                record.history_date, "created", "Leg created",
                actor=actor, actor_kind=actor_kind, severity="quiet",
                details=[_text(f"{leg.pickup_location} → {leg.dropoff_location}")],
            ))
            continue

        older = records[index + 1]
        try:
            delta = record.diff_against(older, foreign_keys_are_objs=True)
            changes = list(delta.changes)
        except Exception as e:
            logger.warning(f"History diff failed for leg {leg.id}: {e}")
            continue

        by_field = {c.field: c for c in changes}
        touched = {f for f in by_field if f not in NOISE_FIELDS}
        if not touched:
            continue

        record_events = []

        # ── the pickup move, possibly recorded late ──
        moved = touched & PICKUP_FIELDS
        if moved:
            stamped_at = by_field.get("pickup_time_changed_at")
            real_at = record.history_date
            late = False
            if stamped_at is not None and stamped_at.new:
                gap = record.history_date - stamped_at.new
                if gap > LATE_RECORD_THRESHOLD:
                    # The move stamped its own clock long before this snapshot
                    # existed. The snapshot is a bystander; so is its user.
                    real_at = stamped_at.new
                    late = True

            details = []
            day_moved = False
            if "pickup_date" in by_field:
                c = by_field["pickup_date"]
                details.append(_detail("Date", _fmt_date(c.old), _fmt_date(c.new)))
                day_moved = True
            if "pickup_time" in by_field:
                c = by_field["pickup_time"]
                details.append(_detail("Time", _fmt_time(c.old), _fmt_time(c.new)))

            record_events.append(_event(
                real_at, "pickup_moved",
                "Pickup date moved" if day_moved else "Pickup time moved",
                actor=None if late else actor,
                actor_kind="system" if late else actor_kind,
                severity="critical" if day_moved else "warn",
                details=details, note=reason or None,
                flags=(["late_recorded"] if late else []) + (["day_moved"] if day_moved else []),
            ))

        # ── driver assignment / removal ──
        # One human action. The six pay/stamp columns it writes are folded into
        # the single line that matters, plus one pay figure.
        if "driver" in touched:
            c = by_field["driver"]
            old_d, new_d = _clean(c.old), _clean(c.new)
            if new_d and not old_d:
                title, kind, sev = f"Assigned to {new_d}", "driver_assigned", "info"
            elif old_d and not new_d:
                title, kind, sev = f"{old_d} removed from the trip", "driver_unassigned", "warn"
            else:
                title, kind, sev = f"Driver changed: {old_d} → {new_d}", "driver_changed", "warn"

            pay = by_field.get("driver_pay_amount")
            details = []
            if pay is not None:
                if pay.new and not pay.old:
                    details.append(_text(f"Driver pay ${pay.new}"))
                elif pay.old and not pay.new:
                    details.append(_text(f"Pay of ${pay.old} cleared"))
                elif pay.old != pay.new:
                    details.append(_detail("Driver pay", f"${pay.old}", f"${pay.new}"))
            record_events.append(_event(
                record.history_date, kind, title, actor=actor,
                actor_kind=actor_kind, severity=sev, details=details,
                note=reason or None,
            ))

        # ── pay edited on its own ──
        # No driver change alongside it, so this is somebody deliberately
        # changing what a driver gets paid — the opposite of noise.
        elif touched & PAY_FIELDS:
            edited = sorted(touched & PAY_FIELDS)
            record_events.append(_event(
                record.history_date, "pay_changed", "Driver pay changed",
                actor=actor, actor_kind=actor_kind, severity="warn",
                details=[
                    _detail(_label(f), by_field[f].old, by_field[f].new)
                    for f in edited
                ],
                note=reason or None,
            ))

        # ── status ──
        if "status" in touched:
            c = by_field["status"]
            status_event = _event(
                record.history_date, "status",
                f"Status: {c.old} → {c.new}", actor=actor, actor_kind=actor_kind,
                severity="info", note=reason or None,
            )
            status_event["status_value"] = c.new
            record_events.append(status_event)

        # ── everything else worth naming ──
        rest = touched - PICKUP_FIELDS - DRIVER_FIELDS - {"status"} - PAY_FIELDS - DERIVED_FIELDS
        if rest:
            details = [
                _detail(_label(f), by_field[f].old, by_field[f].new)
                for f in sorted(rest)
            ]
            record_events.append(_event(
                record.history_date, "field_change",
                "Details updated" if len(rest) > 1 else f"{_label(next(iter(rest)))} updated",
                actor=actor, actor_kind=actor_kind,
                severity="warn" if rest & MONEY_FIELDS else "quiet",
                details=details, note=reason or None,
            ))

        # Folded-away columns stay reachable behind the row's own toggle, so
        # the view is readable by default without anything being lost.
        folded = sorted(
            f for f in touched
            if f in DERIVED_FIELDS or (f in PAY_FIELDS and "driver" in touched)
        )
        if folded and record_events:
            record_events[0]["hidden_details"] = [
                _detail(_label(f), by_field[f].old, by_field[f].new) for f in folded
            ]

        events.extend(record_events)

    return events


# ─────────────────────────── source 2: audit log ───────────────────────────

def _audit_events(leg, existing):
    """
    AuditLog rows that no snapshot already accounts for.

    Every pickup move writes here with the real user and the real clock, which
    makes this the trail that survives when the snapshot trail lies. Rows that
    already have a matching timeline event are dropped so a single move isn't
    reported twice.
    """
    from reservations.models import AuditLog

    rows = AuditLog.objects.filter(
        model_name="Leg", object_id=leg.id
    ).select_related("user").order_by("-timestamp")

    events = []
    for row in rows:
        field = row.field_name or ""
        matched = any(
            e["kind"] == "pickup_moved"
            and abs((e["at"] - row.timestamp)) <= DEDUPE_WINDOW
            for e in existing
        ) if field in PICKUP_FIELDS else any(
            abs((e["at"] - row.timestamp)) <= DEDUPE_WINDOW for e in existing
        )

        if matched and field in PICKUP_FIELDS:
            # Same move — donate the attribution the snapshot trail lacks.
            for e in existing:
                if e["kind"] == "pickup_moved" and abs(e["at"] - row.timestamp) <= DEDUPE_WINDOW:
                    if not e["actor"] and row.user:
                        e["actor"] = _actor_name(row.user)
                        e["actor_kind"] = "staff"
                        if "late_recorded" in e["flags"]:
                            e["flags"].append("actor_recovered")
                    elif not e["actor"] and (row.username == "guest"):
                        e["actor"] = "Guest"
                        e["actor_kind"] = "guest"
                    if not e["note"] and row.notes:
                        e["note"] = row.notes
            continue
        if matched:
            continue

        events.append(_event(
            row.timestamp,
            "pickup_moved" if field in PICKUP_FIELDS else "audit",
            f"{_label(field)} changed" if field else f"Leg {row.action}",
            actor=_actor_name(row.user) or ("Guest" if row.username == "guest" else None),
            actor_kind="staff" if row.user else ("guest" if row.username == "guest" else "system"),
            severity="warn" if field in PICKUP_FIELDS else "quiet",
            details=[_detail(_label(field), row.old_value, row.new_value)] if field else [],
            source="audit", note=row.notes or None,
        ))
    return events


# ───────────────────── source 3: dispatcher flight matches ─────────────────

def _flight_match_events(leg, existing):
    """
    StaffActivity FLIGHT_MATCHED rows. These name the dispatcher who pressed
    Match, which is the question the leg history could never answer. Attached
    to the matching pickup event where one exists, emitted standalone where not.
    """
    from ops.models import StaffActivity

    rows = (
        StaffActivity.objects
        .filter(action_type=StaffActivity.ActionType.FLIGHT_MATCHED)
        .select_related("user")
        .order_by("-created_at")
    )

    events = []
    for row in rows:
        meta = row.metadata or {}
        if str(meta.get("leg_id")) != str(leg.id):
            continue

        attached = False
        for e in existing:
            if e["kind"] == "pickup_moved" and abs(e["at"] - row.created_at) <= DEDUPE_WINDOW:
                e["note"] = e["note"] or "Flight match"
                e["kind"] = "flight_match"
                if not e["actor"] and row.user:
                    e["actor"] = _actor_name(row.user)
                    e["actor_kind"] = "staff"
                    e["flags"].append("actor_recovered")
                attached = True
        if attached:
            continue

        details = []
        if meta.get("old_time") and meta.get("new_time"):
            details.append(_detail("Time", meta["old_time"], meta["new_time"]))
        if meta.get("day_moved"):
            details.append(_detail("Date", meta.get("old_date"), meta.get("new_date")))
        events.append(_event(
            row.created_at, "flight_match", "Pickup matched to flight",
            actor=_actor_name(row.user), actor_kind="staff",
            severity="critical" if meta.get("day_moved") else "warn",
            details=details, source="activity", note="Flight match",
        ))
    return events


# ───────────────────────── source 4: driver status taps ────────────────────

def _leg_status_events(leg, existing):
    """LegStatus rows the snapshot trail didn't already report."""
    from reservations.models import LegStatus

    rows = (
        LegStatus.objects.filter(leg=leg)
        .select_related("updated_by")
        .order_by("-timestamp")
    )
    events = []
    for row in rows:
        if any(e["kind"] == "status" and abs(e["at"] - row.timestamp) <= DEDUPE_WINDOW
               for e in existing):
            continue
        event = _event(
            row.timestamp, "status", f"Marked {row.status}",
            actor=_actor_name(row.updated_by),
            actor_kind="driver" if row.updated_by else "system",
            severity="quiet", source="status",
        )
        event["status_value"] = row.status
        events.append(event)
    return events


# ──────────────────────────────── assembly ─────────────────────────────────

# Below this many consecutive taps, the individual rows are still worth seeing.
STATUS_RUN_MIN = 3


def _collapse_status_runs(events):
    """
    Fold a driver's consecutive status taps into a single row.

    A driver working a trip normally taps on-the-way → on-location → picked-up
    → completed. Four near-identical "Marked X" rows push the changes somebody
    actually *decided* off the screen, which is the opposite of what this page
    is for. The run becomes one row with the steps listed under it.

    Expects `events` newest-first with `day_key` already set.
    """
    out, i = [], 0
    while i < len(events):
        head = events[i]
        if head["kind"] != "status":
            out.append(head)
            i += 1
            continue

        run, j = [], i
        while (
            j < len(events)
            and events[j]["kind"] == "status"
            and events[j]["actor"] == head["actor"]
            and events[j]["day_key"] == head["day_key"]
        ):
            run.append(events[j])
            j += 1

        if len(run) < STATUS_RUN_MIN:
            out.extend(run)
        else:
            steps = list(reversed(run))          # oldest → newest, as it was driven
            first = steps[0].get("status_value") or "started"
            last = steps[-1].get("status_value") or "finished"
            merged = _event(
                head["at"], "status_run", f"Drove the trip: {first} → {last}",
                actor=head["actor"], actor_kind=head["actor_kind"],
                severity="quiet", source=head["source"],
                details=[
                    _text(f"{strf(s['local'], '%-I:%M %p')}   {s.get('status_value') or s['title']}")
                    for s in steps
                ],
            )
            merged["local"] = head["local"]
            merged["day_key"] = head["day_key"]
            out.append(merged)
        i = j
    return out


def build_leg_timeline(leg):
    """
    Merged, newest-first timeline for one leg.

    Order matters: snapshots form the spine, then each remaining source is
    given the chance to enrich an existing event before it is allowed to add
    one of its own. That is what turns four partial records into one account
    instead of four overlapping ones.
    """
    events = _history_events(leg)
    events += _audit_events(leg, events)
    events += _flight_match_events(leg, events)
    events += _leg_status_events(leg, events)

    events.sort(key=lambda e: e["at"], reverse=True)

    for e in events:
        local = timezone.localtime(e["at"]) if timezone.is_aware(e["at"]) else e["at"]
        e["local"] = local
        e["day_key"] = local.date()

    events = _collapse_status_runs(events)

    # Presentation fields, resolved once here rather than in template logic.
    for e in events:
        e["icon"] = KIND_ICONS.get(e["kind"], "bi-record-circle")
        e["actor_label"] = e["actor"] or "System"
        e["actor_kind_label"] = ACTOR_KIND_LABELS.get(e["actor_kind"], "")
        e["flag_labels"] = [FLAG_LABELS[f] for f in e["flags"] if f in FLAG_LABELS]
    return events


def timeline_summary(events):
    """
    Headline counts for the top of the page — what a dispatcher should know
    before reading a single row.
    """
    return {
        "total": len(events),
        "pickup_moves": sum(1 for e in events if e["kind"] in ("pickup_moved", "flight_match")),
        "day_moves": sum(1 for e in events if "day_moved" in e["flags"]),
        "late_recorded": sum(1 for e in events if "late_recorded" in e["flags"]),
        "driver_changes": sum(
            1 for e in events
            if e["kind"] in ("driver_assigned", "driver_unassigned", "driver_changed")
        ),
    }
