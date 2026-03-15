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


def process_agent_payout(agent, send_email=False, recipient_email=None):
    """
    Process payout for a single agent, optionally email statement.
    Returns (payout, amount, agency_payout).
    """
    payout, amount, agency_payout = agent.process_commission_payment()

    if payout and send_email:
        email = recipient_email or agent.user.email
        _run_in_background(send_agent_commission_statement, agent, payout, email)

    return payout, amount, agency_payout


def process_agency_payout(agency, send_email=False, recipient_email=None):
    """
    Process payout for entire agency, optionally email statement.
    Returns (payout, amount).
    """
    payout, amount = agency.process_agency_commission_payment()

    if payout and send_email:
        email = recipient_email
        if not email:
            first_head = agency.heads.first()
            email = first_head.email if first_head else None
        if email:
            _run_in_background(send_agency_commission_statement, agency, payout, email)

    return payout, amount


def preview_agent_payout(agent):
    """
    Read-only preview of what payout would include.
    Returns dict with reservations, total, period info.
    """
    from reservations.models import Reservation

    unpaid = Reservation.objects.filter(
        travel_agent=agent, commission_paid=False, status="completed"
    ).select_related(
        "customer", "rate__route__origin", "rate__route__destination"
    ).order_by("-created_at")

    if not unpaid.exists():
        return {"reservations": [], "total": Decimal("0"), "count": 0}

    reservations_data = []
    total = Decimal("0")
    earliest_pickup = None

    for res in unpaid:
        commission = res.base_price * (agent.commission_rate / 100)
        total += commission

        route = ""
        if res.rate and res.rate.route:
            route = f"{res.rate.route.origin} to {res.rate.route.destination}"

        for leg in res.legs.all():
            if earliest_pickup is None or leg.pickup_date < earliest_pickup:
                earliest_pickup = leg.pickup_date

        reservations_data.append({
            "id": res.id,
            "customer": res.customer.get_full_name(),
            "route": route,
            "base_price": str(res.base_price),
            "total_price": str(res.total_price),
            "commission": str(commission.quantize(Decimal("0.01"))),
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
    Read-only preview of agency payout with per-agent breakdown.
    Returns dict with agents list, each containing reservations and subtotal.
    """
    from reservations.models import Reservation

    agents = agency.agents.filter(
        unpaid_commissions__gt=0, agency_handles_payment=True
    ).select_related("user")

    if not agents.exists():
        return {"agents": [], "total": "0.00", "count": 0}

    agents_data = []
    grand_total = Decimal("0")
    total_reservations = 0

    for agent in agents:
        unpaid = Reservation.objects.filter(
            travel_agent=agent, commission_paid=False, status="completed"
        ).select_related(
            "customer", "rate__route__origin", "rate__route__destination"
        ).order_by("-created_at")

        if not unpaid.exists():
            continue

        agent_total = Decimal("0")
        res_data = []

        for res in unpaid:
            commission = res.base_price * (agent.commission_rate / 100)
            agent_total += commission

            route = ""
            if res.rate and res.rate.route:
                route = f"{res.rate.route.origin} to {res.rate.route.destination}"

            res_data.append({
                "id": res.id,
                "customer": res.customer.get_full_name(),
                "route": route,
                "base_price": str(res.base_price),
                "commission": str(commission.quantize(Decimal("0.01"))),
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
