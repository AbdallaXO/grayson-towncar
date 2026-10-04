"""Agents who asked to be paid through their agency, but aren't.

An agent who picks "Agency" as their payment method types their agency's name at
sign-up. Nothing links that text to an Agency record, so they land under Direct
Agents with no way to pay them. This finds them and proposes the link.

Only agents whose OWN method is "Agency" are ever touched. An agent who picked
Venmo, PayPal, or anything else is paid directly even when they belong to an
agency -- linking them would send their money to the agency instead.
"""
import difflib
import re
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

READY = "ready"      # their agency exists under exactly the name they typed, or is already linked
CHECK = "check"      # a close spelling match -- a person confirms it's the same agency
MISSING = "missing"  # no agency in the system matches what they typed

# Words that vary between how an agent types an agency and how we saved it.
_FILLER = re.compile(r"\b(?:llc|inc|travel|travels|vacation|vacations|agency|the|co|company|group|by)\b|[^a-z0-9 ]")


def _norm(name):
    return " ".join(_FILLER.sub(" ", (name or "").lower()).split())


def agency_payable(agency):
    """'' when the agency itself can be paid, else what's missing."""
    if not agency.payment_method or agency.payment_method == "agency":
        return "This agency has no payment method yet."
    if not (agency.payment_info or "").strip() and agency.payment_method != "check":
        return "This agency has no payment details yet."
    return ""


@dataclass
class Proposal:
    agent: object
    status: str
    agency: object = None
    note: str = ""
    owed: Decimal = Decimal("0")

    @property
    def typed(self):
        return (self.agent.agency_name or "").strip()


def proposals():
    """Every agent who chose "Agency" but isn't paid through one, with a proposed fix."""
    from users.eligibility import bulk_ready_totals
    from users.models import Agency, TravelAgent

    agents = list(
        TravelAgent.objects.filter(payment_method="agency")
        .filter(Q(agency__isnull=True) | Q(agency_handles_payment=False))
        .select_related("agency", "user")
        .order_by("agent_name")
    )
    agencies = list(Agency.objects.filter(is_active=True))
    by_name, by_norm = {}, {}
    for a in agencies:
        by_name.setdefault(a.name.strip().lower(), []).append(a)
        by_norm.setdefault(_norm(a.name), []).append(a)
    owed = bulk_ready_totals([a.id for a in agents])

    out = []
    for agent in agents:
        typed = (agent.agency_name or "").strip()
        if agent.agency_id and not agent.agency.is_active:
            p = Proposal(agent, CHECK, agent.agency, "Linked to an agency that's switched off.")
        elif agent.agency_id:
            p = Proposal(agent, READY, agent.agency, "Already linked, but \"agency pays\" is off.")
        elif typed and len(by_name.get(typed.lower(), [])) == 1:
            p = Proposal(agent, READY, by_name[typed.lower()][0])
        elif typed and by_name.get(typed.lower()):
            p = Proposal(agent, CHECK, by_name[typed.lower()][0], "Two agencies have this name. Check which one.")
        else:
            close = difflib.get_close_matches(_norm(typed), list(by_norm), n=1, cutoff=0.85) if typed else []
            if close and _norm(typed):
                p = Proposal(agent, CHECK, by_norm[close[0]][0], "Spelled differently. Check it's the same agency.")
            else:
                p = Proposal(agent, MISSING)
        p.owed = owed.get(agent.id, Decimal("0"))
        out.append(p)
    return out


class LinkRefused(Exception):
    """The agent changed since the page was loaded; nothing was written."""


def link_to_agency(agent_id, agency_id, *, user):
    """Route one agent's commission through an agency. Returns the agency.

    Re-checks the agent at write time: if they've since picked another payment
    method, or someone linked them to a different agency, nothing changes.
    """
    from reservations.models import AuditLog
    from users.models import Agency, TravelAgent

    with transaction.atomic():
        # of=("self",): Postgres refuses to lock the empty side of the agency join
        # (most of these agents have no agency yet).
        agent = TravelAgent.objects.select_for_update(of=("self",)).select_related("agency").get(pk=agent_id)
        agency = Agency.objects.get(pk=agency_id)
        name = agent.agent_name or agent.user.get_username()
        if agent.payment_method != "agency":
            raise LinkRefused(f"{name} now gets paid by {agent.get_payment_method_display() or 'no method'}, "
                              "so they were left as they are.")
        if agent.agency_id and agent.agency_id != agency.id:
            raise LinkRefused(f"{name} is now linked to {agent.agency.name}, so they were left as they are.")
        if agent.agency_id == agency.id and agent.agency_handles_payment:
            return agency  # someone already did it

        old = f"{agent.agency.name if agent.agency else 'no agency'}, agency pays {'on' if agent.agency_handles_payment else 'off'}"
        agent.agency = agency
        agent.agency_handles_payment = True
        agent.save(update_fields=["agency", "agency_handles_payment"])
        AuditLog.objects.create(
            model_name="TravelAgent", object_id=agent.id, action="updated", field_name="agency",
            old_value=old, new_value=f"{agency.name}, agency pays on",
            user=user, username=user.get_username() if user else "system",
        )
    return agency


def create_agency_and_link(agent_id, *, user):
    """Add the agency the agent typed (or reuse one with that exact name) and link them to it."""
    from users.models import Agency, TravelAgent

    agent = TravelAgent.objects.get(pk=agent_id)
    typed = (agent.agency_name or "").strip()
    if not typed:
        raise LinkRefused("They didn't type an agency name. Open their profile to set one.")
    with transaction.atomic():
        agency = Agency.objects.filter(name__iexact=typed, is_active=True).first()
        created = agency is None
        if created:
            agency = Agency.objects.create(name=typed, is_active=True)
        link_to_agency(agent.id, agency.id, user=user)
    return agency, created
