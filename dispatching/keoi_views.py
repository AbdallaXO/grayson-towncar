"""KEOI ("Keep Eye On It") AJAX endpoints.

Staff-facing watch-flag CRUD. Follows the board AJAX conventions:
``json.loads(request.body)`` in, ``JsonResponse({"success": bool, ...})`` out,
standard CSRF (@login_required + @require_POST; history is GET).

The lifecycle (auto-close on completion, auto-reactivate on reopen) lives in the
Leg signal pair + ``reservations/keoi.py`` — these endpoints only handle the
manual create / edit / remove surface. Audit rows go through ``create_audit_log``
(authenticated actor + >500-char truncation for description edits); the
system-driven close/reactivate audit rows come from the service module.
"""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from reservations.models import Leg, LegKeoi
from reservations.signals import create_audit_log

logger = logging.getLogger(__name__)

MAX_DESCRIPTION_LEN = 2000


def _fmt_dt(dt):
    """Windows-safe short datetime, e.g. 'Jul 24, 2026 3:45 PM' (local tz)."""
    if not dt:
        return ""
    dt = timezone.localtime(dt)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} {hour}:{dt.strftime('%M %p')}"


def _keoi_payload(k):
    """The dict every save response returns; labels via get_*_display() so the
    frontend never duplicates choice text."""
    return {
        "id": k.id,
        "leg_id": k.leg_id,
        "category": k.category,
        "category_label": k.get_category_display(),
        "operational_status": k.operational_status,
        "status_label": k.get_operational_status_display(),
        "description": k.description,
        "created_by": k.created_by.username if k.created_by else "",
        "created_at_display": _fmt_dt(k.created_at),
        "updated_by": k.updated_by.username if k.updated_by else "",
        "updated_at_display": _fmt_dt(k.updated_at),
    }


def _apply_keoi_edit(keoi, category, desc, op_status, request):
    """Apply an edit to an existing active flag, writing one audit row per
    changed field. Returns True if anything changed (and was saved)."""
    changed = False
    if keoi.category != category:
        create_audit_log(
            model_name="Leg", object_id=keoi.leg_id, action="updated",
            user=request.user, field_name="keoi_category",
            old_value=keoi.category, new_value=category, request=request,
            notes="KEOI category changed",
        )
        keoi.category = category
        changed = True
    if keoi.description != desc:
        create_audit_log(
            model_name="Leg", object_id=keoi.leg_id, action="updated",
            user=request.user, field_name="keoi_description",
            old_value=keoi.description, new_value=desc, request=request,
            notes="KEOI description changed",
        )
        keoi.description = desc
        changed = True
    if keoi.operational_status != op_status:
        create_audit_log(
            model_name="Leg", object_id=keoi.leg_id, action="status_changed",
            user=request.user, field_name="keoi_operational_status",
            old_value=keoi.operational_status, new_value=op_status, request=request,
            notes="KEOI operational status changed",
        )
        keoi.operational_status = op_status
        changed = True
    if changed:
        keoi.updated_by = request.user
        keoi.save(update_fields=["category", "description", "operational_status",
                                 "updated_by", "updated_at"])
    return changed


@login_required
@require_POST
def keoi_save(request):
    """Create or edit the active KEOI flag for a leg (upsert, addressed by
    leg_id — the partial unique constraint makes 'the active KEOI for leg N'
    unambiguous)."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    leg_id = data.get("leg_id")
    # Coerce to str before .strip(): a non-string JSON value (e.g. a number or
    # list) must yield a clean 400, not an uncaught AttributeError -> HTTP 500.
    category = str(data.get("category") or "").strip()
    op_status = data.get("operational_status")
    desc = str(data.get("description") or "").strip()

    try:
        leg = Leg.objects.get(id=leg_id)
    except (Leg.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    if category not in LegKeoi.Category.values:
        return JsonResponse({"success": False, "error": "Invalid category"}, status=400)

    if not desc:
        return JsonResponse(
            {"success": False,
             "error": "Description is required — say what to watch and what to do."},
            status=400,
        )
    if len(desc) > MAX_DESCRIPTION_LEN:
        return JsonResponse(
            {"success": False,
             "error": f"Description is too long ({MAX_DESCRIPTION_LEN} character max)."},
            status=400,
        )

    if op_status in (None, ""):
        op_status = LegKeoi.OperationalStatus.NEEDS_ATTENTION
    elif op_status not in LegKeoi.OperationalStatus.values:
        return JsonResponse({"success": False, "error": "Invalid operational status"}, status=400)

    # Terminal-leg guard: a flag on a completed/cancelled leg would auto-close
    # instantly (mirrors the cancelled-leg driver-assign guard in views.py).
    if leg.status in LegKeoi.TERMINAL_LEG_STATUSES:
        return JsonResponse(
            {"success": False, "error": "Cannot flag a completed or cancelled leg."},
            status=400,
        )

    with transaction.atomic():
        existing = LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).first()
        if existing is not None:
            created = False
            keoi = existing
            _apply_keoi_edit(keoi, category, desc, op_status, request)
        else:
            try:
                with transaction.atomic():   # savepoint so the double-create race
                    keoi = LegKeoi.objects.create(   # doesn't poison the outer txn
                        leg=leg, category=category, description=desc,
                        operational_status=op_status,
                        created_by=request.user, updated_by=request.user,
                    )
                created = True
                create_audit_log(
                    model_name="Leg", object_id=leg.id, action="created",
                    user=request.user, field_name="keoi",
                    old_value=None, new_value=category, request=request,
                    notes=f"KEOI flag raised: {keoi.get_category_display()}",
                )
            except IntegrityError:
                # A concurrent request created the active flag first; treat as edit.
                created = False
                keoi = LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).first()
                if keoi is None:
                    raise
                _apply_keoi_edit(keoi, category, desc, op_status, request)

    return JsonResponse({"success": True, "created": created, "keoi": _keoi_payload(keoi)})


@login_required
@require_POST
def keoi_remove(request):
    """Remove (close as admin_removed) the active KEOI flag with a required
    reason. Gated on the reservations.remove_keoi permission — superusers pass
    automatically; managers can delegate via the admin without a deploy."""
    if not request.user.has_perm("reservations.remove_keoi"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    leg_id = data.get("leg_id")
    reason = str(data.get("reason") or "").strip()   # coerce -> clean 400, not 500
    if not reason:
        return JsonResponse(
            {"success": False, "error": "A reason is required to remove a KEOI flag."},
            status=400,
        )
    if len(reason) > MAX_DESCRIPTION_LEN:
        return JsonResponse(
            {"success": False,
             "error": f"Reason is too long ({MAX_DESCRIPTION_LEN} character max)."},
            status=400,
        )

    try:
        leg = Leg.objects.get(id=leg_id)
    except (Leg.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    keoi = LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).first()
    if keoi is None:
        return JsonResponse(
            {"success": False, "error": "No active KEOI flag on this leg."}, status=404)

    with transaction.atomic():
        keoi.closed_at = timezone.now()
        keoi.closed_reason = LegKeoi.ClosedReason.ADMIN_REMOVED
        keoi.closed_by = request.user
        keoi.removal_reason = reason
        keoi.save(update_fields=["closed_at", "closed_reason", "closed_by",
                                 "removal_reason", "updated_at"])
        create_audit_log(
            model_name="Leg", object_id=leg.id, action="updated",
            user=request.user, field_name="keoi_removed",
            old_value="active", new_value="admin_removed", request=request,
            notes=f"KEOI removed by {request.user.username}: {reason}",
        )

    return JsonResponse({"success": True, "keoi": None, "leg_id": leg.id})


@login_required
@require_http_methods(["GET"])
def keoi_history(request):
    """All KEOI rows for a leg, newest first — powers the modal's 'previous
    flags' section."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    leg_id = request.GET.get("leg_id")
    try:
        leg = Leg.objects.get(id=leg_id)
    except (Leg.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    rows = (LegKeoi.objects.filter(leg=leg)
            .select_related("created_by", "updated_by", "closed_by")
            .order_by("-created_at"))
    flags = [{
        "id": k.id,
        "category": k.category,
        "category_label": k.get_category_display(),
        "operational_status": k.operational_status,
        "status_label": k.get_operational_status_display(),
        "description": k.description,
        "is_active": k.is_active,
        "created_by": k.created_by.username if k.created_by else "",
        "created_at_display": _fmt_dt(k.created_at),
        "updated_by": k.updated_by.username if k.updated_by else "",
        "updated_at_display": _fmt_dt(k.updated_at),
        "closed_reason": k.closed_reason or "",
        "closed_reason_label": k.get_closed_reason_display() if k.closed_reason else "",
        "closed_by": k.closed_by.username if k.closed_by else "",
        "closed_at_display": _fmt_dt(k.closed_at),
        "removal_reason": k.removal_reason,
    } for k in rows]

    return JsonResponse({"success": True, "flags": flags})
