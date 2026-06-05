"""
Service layer for commission payout processing.
Thin orchestration wrapper around existing model methods.
Used by both the Affiliate Payment Dashboard and Django admin actions.
"""
import logging
from decimal import Decimal

from django.db.models import Sum, F, ExpressionWrapper, DecimalField
from django.utils import timezone

from reservations.utils import _run_in_background
from users.emails import send_agent_commission_statement, send_agency_commission_statement

logger = logging.getLogger(__name__)


def _apply_reference_and_method(payout, *, payment_reference, payment_method_used, fallback_method):
    """Stamp a freshly created payout with the operator-supplied reference + method."""
    if not payout:
        return
    reference = (payment_reference or "").strip()
    method = (payment_method_used or fallback_method or "").strip()
    if reference or method:
        payout.payment_reference = reference
        payout.payment_method_used = method
        payout.save(update_fields=["payment_reference", "payment_method_used"])


def _log_payout_audit(payout, *, sent_by):
    """Write one AuditLog row for a freshly created payout, attributed to the operator."""
    if not payout:
        return
    from reservations.models import AuditLog  # late import to avoid circular

    model_name = payout.__class__.__name__
    summary_parts = [f"amount=${payout.total_amount}"]
    if payout.payment_method_used:
        summary_parts.append(f"method={payout.payment_method_used}")
    if payout.payment_reference:
        summary_parts.append(f"ref={payout.payment_reference}")
    if model_name == "CommissionPayout":
        summary_parts.append(f"agent_id={payout.agent_id}")
        if payout.agency_id:
            summary_parts.append(f"agency_id={payout.agency_id}")
    elif model_name == "AgencyCommissionPayout":
        summary_parts.append(f"agency_id={payout.agency_id}")

    try:
        AuditLog.objects.create(
            model_name=model_name,
            object_id=payout.id,
            action="commission_processed",
            new_value=" ".join(summary_parts),
            user=sent_by,
            username=getattr(sent_by, "username", "system") if sent_by else "system",
        )
    except Exception:  # noqa: BLE001 — audit must not break the payout flow
        logger.exception("Failed to write AuditLog for %s #%s", model_name, payout.id)


def process_agent_payout(
    agent,
    *,
    send_email=False,
    recipient_email=None,
    sent_by=None,
    payment_reference="",
    payment_method_used="",
):
    """Process payout for a single agent, optionally email statement.

    Returns (payout, amount, agency_payout). When payment_reference or
    payment_method_used is given, both the agent payout and any spawned agency
    payout are stamped with those values so they show up on the audit trail.
    """
    payout, amount, agency_payout = agent.process_commission_payment()

    if payout:
        _apply_reference_and_method(
            payout,
            payment_reference=payment_reference,
            payment_method_used=payment_method_used,
            fallback_method=agent.effective_payment_method,
        )
        _log_payout_audit(payout, sent_by=sent_by)

    if agency_payout:
        _apply_reference_and_method(
            agency_payout,
            payment_reference=payment_reference,
            payment_method_used=payment_method_used,
            fallback_method=(agent.agency.payment_method if agent.agency else ""),
        )
        _log_payout_audit(agency_payout, sent_by=sent_by)

    if payout and send_email:
        email = recipient_email or agent.user.email
        _run_in_background(send_agent_commission_statement, agent, payout, email, sent_by=sent_by)

    return payout, amount, agency_payout


def process_agency_payout(
    agency,
    *,
    send_email=False,
    recipient_email=None,
    sent_by=None,
    payment_reference="",
    payment_method_used="",
):
    """Process payout for entire agency, optionally email statement.

    Returns (payout, amount).
    """
    payout, amount = agency.process_agency_commission_payment()

    if payout:
        _apply_reference_and_method(
            payout,
            payment_reference=payment_reference,
            payment_method_used=payment_method_used,
            fallback_method=agency.payment_method,
        )
        _log_payout_audit(payout, sent_by=sent_by)
        # Stamp every child agent payout too so per-agent history stays consistent.
        for child in payout.agent_payouts.all():
            _apply_reference_and_method(
                child,
                payment_reference=payment_reference,
                payment_method_used=payment_method_used,
                fallback_method=agency.payment_method,
            )
            _log_payout_audit(child, sent_by=sent_by)

    if payout and send_email:
        email = recipient_email
        if not email:
            first_head = agency.heads.first()
            email = first_head.email if first_head else None
        if email:
            _run_in_background(send_agency_commission_statement, agency, payout, email, sent_by=sent_by)

    return payout, amount


def process_bulk_payouts(items, *, sent_by):
    """Process a list of mark-paid actions atomically per-item.

    items: list of dicts. Each must have:
        - "type": "agent" | "agency"
        - "id":   int
        - "reference": str (optional, defaults to "")
        - "method":    str (optional, defaults to the payee's stored method)
        - "email":     bool (optional)

    Each item runs in its own transaction so one failure does not roll back successes.
    Returns a list of result dicts, one per input item, in the same order.
    """
    from users.models import TravelAgent, Agency

    results = []
    for item in items:
        kind = item.get("type")
        try:
            obj_id = int(item.get("id"))
        except (TypeError, ValueError):
            results.append({"ok": False, "id": item.get("id"), "error": "Invalid id."})
            continue

        reference = item.get("reference") or ""
        method = item.get("method") or ""
        email = bool(item.get("email"))

        try:
            if kind == "agent":
                agent = TravelAgent.objects.select_related("user", "agency").get(id=obj_id)
                if agent.calculate_unpaid_commissions() <= 0:
                    results.append({
                        "ok": False, "type": "agent", "id": obj_id,
                        "name": agent.agent_name or agent.user.get_username(),
                        "error": "No unpaid commissions.",
                    })
                    continue
                payout, amount, agency_payout = process_agent_payout(
                    agent,
                    sent_by=sent_by,
                    payment_reference=reference,
                    payment_method_used=method,
                    send_email=email,
                    recipient_email=agent.user.email,
                )
                results.append({
                    "ok": True, "type": "agent", "id": obj_id,
                    "name": agent.agent_name or agent.user.get_username(),
                    "amount": str(amount or Decimal("0")),
                    "payout_id": payout.id if payout else None,
                    "agency_payout_id": agency_payout.id if agency_payout else None,
                })

            elif kind == "agency":
                agency = Agency.objects.get(id=obj_id)
                # Live eligibility check -- don't trust the cached
                # unpaid_commissions stat. A stale stat could either incorrectly
                # block a payout that has Ready items, or claim there are items
                # when eligibility actually says nothing's Ready.
                from users.eligibility import sum_ready
                owing_agents = sum(
                    1 for a in agency.agents.filter(agency_handles_payment=True)
                    if sum_ready(a) > 0
                )
                if owing_agents == 0:
                    results.append({
                        "ok": False, "type": "agency", "id": obj_id, "name": agency.name,
                        "error": "No commissions ready to pay in this agency.",
                    })
                    continue
                recipient_email = None
                if email:
                    first_head = agency.heads.first()
                    recipient_email = first_head.email if first_head else None
                payout, amount = process_agency_payout(
                    agency,
                    sent_by=sent_by,
                    payment_reference=reference,
                    payment_method_used=method,
                    send_email=email,
                    recipient_email=recipient_email,
                )
                results.append({
                    "ok": True, "type": "agency", "id": obj_id, "name": agency.name,
                    "amount": str(amount or Decimal("0")),
                    "payout_id": payout.id if payout else None,
                    "agents_count": payout.agent_payouts.count() if payout else 0,
                })

            else:
                results.append({"ok": False, "id": obj_id, "error": f"Unknown type: {kind!r}"})

        except (TravelAgent.DoesNotExist, Agency.DoesNotExist):
            results.append({"ok": False, "type": kind, "id": obj_id, "error": "Not found."})
        except Exception as exc:  # noqa: BLE001 — log every failure, keep going
            logger.exception("Bulk payout failed for %s #%s", kind, obj_id)
            results.append({"ok": False, "type": kind, "id": obj_id, "error": str(exc)})

    return results


def preview_agent_payout(agent):
    """
    Read-only preview of what an agent payout would include RIGHT NOW.

    Uses users.eligibility.ready_reservations so the preview is a perfect
    mirror of what process_commission_payment would actually pay -- never
    showing reservations that the queue marks as Review/Excluded.
    """
    from users.eligibility import ready_reservations

    ready_items = list(ready_reservations(agent))

    if not ready_items:
        return {"reservations": [], "total": "0.00", "count": 0}

    reservations_data = []
    total = Decimal("0")
    earliest_pickup = None

    for res, result in ready_items:
        total += result.commission

        route = ""
        if res.rate and res.rate.route:
            route = f"{res.rate.route.origin} to {res.rate.route.destination}"

        for leg in res.legs.all():
            if leg.pickup_date and (earliest_pickup is None or leg.pickup_date < earliest_pickup):
                earliest_pickup = leg.pickup_date

        reservations_data.append({
            "id": res.id,
            "display_number": res.display_number,
            "uuid": str(res.uuid),
            "customer": res.customer.get_full_name(),
            "route": route,
            "base_price": str(res.base_price),
            "total_price": str(res.total_price),
            "commission": str(result.commission),
            "date": res.created_at.strftime("%b %d, %Y"),
        })

    return {
        "reservations": reservations_data,
        "total": str(total.quantize(Decimal("0.01"))),
        "count": len(reservations_data),
        "period_start": str(earliest_pickup) if earliest_pickup else str(timezone.localtime(timezone.now()).date()),
        "period_end": str(timezone.localtime(timezone.now()).date()),
    }


def preview_agency_payout(agency):
    """
    Read-only preview of an agency payout with per-agent breakdown.

    Only includes agents who currently have at least one Ready reservation --
    pulls live via the eligibility helper rather than trusting the cached
    unpaid_commissions stat (which can drift as time passes).
    """
    from users.eligibility import ready_reservations

    # Don't pre-filter on the cached unpaid_commissions stat -- it can be
    # stale (e.g. a trip just crossed its grace threshold an hour ago and the
    # stat hasn't been recalculated yet).
    agents = agency.agents.filter(agency_handles_payment=True).select_related("user")

    if not agents.exists():
        return {"agents": [], "total": "0.00", "count": 0}

    agents_data = []
    grand_total = Decimal("0")
    total_reservations = 0

    for agent in agents:
        ready_items = list(ready_reservations(agent))
        if not ready_items:
            continue

        agent_total = Decimal("0")
        res_data = []

        for res, result in ready_items:
            agent_total += result.commission

            route = ""
            if res.rate and res.rate.route:
                route = f"{res.rate.route.origin} to {res.rate.route.destination}"

            res_data.append({
                "id": res.id,
                "display_number": res.display_number,
                "customer": res.customer.get_full_name(),
                "route": route,
                "base_price": str(res.base_price),
                "commission": str(result.commission),
                "date": res.created_at.strftime("%b %d, %Y"),
            })

        grand_total += agent_total
        total_reservations += len(res_data)

        agents_data.append({
            "agent_name": agent.agent_name or agent.user.username,
            "email": agent.user.email,
            "commission_rate": str(agent.commission_rate),
            "reservations": res_data,
            "subtotal": str(agent_total.quantize(Decimal("0.01"))),
            "count": len(res_data),
        })

    return {
        "agents": agents_data,
        "total": str(grand_total.quantize(Decimal("0.01"))),
        "count": total_reservations,
        "agents_count": len(agents_data),
    }
