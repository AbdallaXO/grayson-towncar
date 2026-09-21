"""Price a custom-route quote request the moment it arrives.

A guest asks the website for a route the rate card cannot quote. The price
itself is not a judgement call — the quote calculator's engine gives the same
answer to everyone — so it is worked out here, in the background, right after
the lead is created, and put where the person who will answer the guest can see
it: a note on the GoHighLevel contact ("Suggested price: $185 …") and a line on
the lead's activity log. The automatic first text still goes out and the guest's
reply lands in GoHighLevel, which the team sweeps all day; whoever picks it up
has the number on the card.

Founder decision 2026-09-21: the QUOTE NEEDED ops task that used to be filed for
these requests is retired. `price_quote_task` below still prices the ones that
are already open; `price_lead` is what new requests use.

Nothing here is customer-facing. `Lead.estimated_price` and `Quote.estimated_price`
are left alone on purpose: they mean "the website quoted this", and it did not.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from .models import OperationalTask

logger = logging.getLogger(__name__)

PRICE_META_KEYS = (
    "suggested_price", "price_source_label", "price_card_route", "distance_text",
    "duration_text", "price_internal", "price_notes", "gratuity_mandatory",
    "priced_vehicle", "priced_at", "price_error",
)


def money(value):
    """'185.00' -> '185'; '187.50' -> '187.50'. The text should never say $185.00."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    return str(int(d)) if d == d.to_integral_value() else f"{d:.2f}"


def price_quote_task(task_id):
    """Work out the price for one quote-needed task and store it on the task.

    Returns the pricing payload, or None when there was nothing to price or the
    engine could not price it (the reason is stored as `price_error` so the page
    can say so instead of spinning).
    """
    task = (
        OperationalTask.objects.select_related("lead", "lead__vehicle")
        .filter(id=task_id)
        .first()
    )
    if task is None or task.lead_id is None:
        return None
    lead = task.lead
    meta = dict(task.metadata or {})
    now = timezone.now()

    if not (lead.pickup_location and lead.dropoff_location):
        meta["price_error"] = "The request has no route to price."
        meta["priced_at"] = now.isoformat()
        task.metadata = meta
        task.save(update_fields=["metadata", "updated_at"])
        return None

    from dispatching.views import price_trip

    vehicle_type = lead.vehicle.vehicle_type if lead.vehicle_id else "towncar"
    trip_type = "roundtrip" if lead.trip_type == "roundtrip" else "oneway"
    result = price_trip(
        lead.pickup_location, lead.dropoff_location, vehicle_type, trip_type,
        use_cache=True,
    )

    for key in PRICE_META_KEYS:
        meta.pop(key, None)
    meta["priced_at"] = now.isoformat()
    meta["priced_vehicle"] = vehicle_type

    if not result or result.get("error") or not result.get("price"):
        meta["price_error"] = (result or {}).get("error") or "Could not price this trip."
        task.metadata = meta
        task.save(update_fields=["metadata", "updated_at"])
        logger.info("Quote task #%s not priced: %s", task.id, meta["price_error"])
        return None

    amount = money(result["price"])
    meta.update({
        "suggested_price": amount,
        "price_source_label": result.get("source_label") or "Estimate",
        "price_card_route": result.get("card_route"),
        "distance_text": result.get("distance_text"),
        "duration_text": result.get("duration_text"),
        "price_internal": result.get("internal") or {},
        "price_notes": result.get("notes") or [],
        "gratuity_mandatory": bool(result.get("gratuity_mandatory")),
    })
    task.metadata = meta

    # The number goes where the queue can see it: the title. Re-pricing
    # replaces the old figure rather than stacking a second one.
    base_title = task.title.split(" · $")[0]
    task.title = f"{base_title} · ${amount}"[:200]

    where = ", ".join(
        p for p in (result.get("distance_text"), result.get("duration_text"))
        if p and p not in ("N/A", "n/a")
    )
    line = f"Suggested price: ${amount} ({meta['price_source_label']}"
    line += f", {where})" if where else ")"
    desc = task.description or ""
    desc = "\n".join(l for l in desc.split("\n") if not l.startswith("Suggested price:"))
    task.description = (desc.rstrip() + "\n" + line).strip()

    task.save(update_fields=["metadata", "title", "description", "updated_at"])
    logger.info("Quote task #%s priced at $%s (%s)", task.id, amount, meta["price_source_label"])
    return result


def price_quote_task_in_background(task_id):
    """Fire-and-forget from the public quote form: the guest's submission must
    never wait on a Distance Matrix call, and a pricing failure must never
    cost a lead."""
    from reservations.utils import _run_in_background

    _run_in_background(price_quote_task, task_id)


GHL_CONTACT_WAIT_TRIES = 6       # the contact is created in another thread;
GHL_CONTACT_WAIT_SECONDS = 5     # give it up to ~30 s before giving up on the note


def price_lead(lead_id, *, wait_for_contact=True):
    """Work out the price for one custom-route lead, write it on the lead's
    activity log, and leave it as a note on the GoHighLevel contact.

    Returns the pricing payload, or None when the lead has no route or the
    engine could not price it (the activity line says so either way).
    """
    import time as _time

    from ghl_integration.models import LeadActivity
    from reservations.models import Lead

    lead = Lead.objects.select_related("vehicle").filter(id=lead_id).first()
    if lead is None:
        return None
    if not (lead.pickup_location and lead.dropoff_location):
        return None

    from dispatching.views import price_trip

    vehicle_type = lead.vehicle.vehicle_type if lead.vehicle_id else "towncar"
    trip_type = "roundtrip" if lead.trip_type == "roundtrip" else "oneway"
    result = price_trip(
        lead.pickup_location, lead.dropoff_location, vehicle_type, trip_type,
        use_cache=True,
    )

    if not result or result.get("error") or not result.get("price"):
        reason = (result or {}).get("error") or "Could not price this trip."
        LeadActivity.objects.create(
            lead=lead, activity_type=LeadActivity.ActivityType.STATUS_CHANGE,
            description=f"No suggested price: {reason} Price it in the quote calculator.",
            metadata={"price_error": reason, "priced_vehicle": vehicle_type},
        )
        logger.info("Lead #%s not priced: %s", lead.id, reason)
        return None

    amount = money(result["price"])
    source = result.get("source_label") or "Estimate"
    where = ", ".join(
        p for p in (result.get("distance_text"), result.get("duration_text"))
        if p and p not in ("N/A", "n/a")
    )
    ride = f"{'round-trip' if trip_type == 'roundtrip' else 'one-way'} {lead.vehicle.get_vehicle_type_display() if lead.vehicle_id else 'ride'}"
    line = f"Suggested price: ${amount} — {ride} {lead.pickup_location} → {lead.dropoff_location}"
    if lead.pickup_date:
        line += f" on {lead.pickup_date.strftime('%b')} {lead.pickup_date.day}"
    line += f" ({source}" + (f", {where})" if where else ")")
    if result.get("gratuity_mandatory"):
        line += " · gratuity is added on this one"

    LeadActivity.objects.create(
        lead=lead, activity_type=LeadActivity.ActivityType.STATUS_CHANGE,
        description=line,
        metadata={
            "suggested_price": amount, "price_source_label": source,
            "distance_text": result.get("distance_text"), "duration_text": result.get("duration_text"),
            "price_internal": result.get("internal") or {}, "price_notes": result.get("notes") or [],
            "priced_vehicle": vehicle_type, "priced_at": timezone.now().isoformat(),
        },
    )

    # The GoHighLevel contact is created by another background thread right
    # after the lead; wait a little for its id, then leave the note.
    contact_id = lead.ghl_contact_id
    tries = GHL_CONTACT_WAIT_TRIES if wait_for_contact else 1
    for _ in range(tries):
        if contact_id:
            break
        _time.sleep(GHL_CONTACT_WAIT_SECONDS if wait_for_contact else 0)
        contact_id = Lead.objects.filter(id=lead.id).values_list("ghl_contact_id", flat=True).first()
    if contact_id:
        from ghl_integration.services import GoHighLevelService
        note = (
            f"{line}\n\nThe website had no online rate for this route, so nothing was "
            f"quoted automatically. This is the dispatcher quote calculator's number — "
            f"send it, or adjust it, when they reply."
        )
        try:
            GoHighLevelService().add_note(contact_id, note)
        except Exception as exc:
            logger.warning("Could not leave the price note on GHL contact %s: %s", contact_id, exc)
    else:
        logger.info("Lead #%s priced at $%s but has no GHL contact yet; note skipped", lead.id, amount)

    logger.info("Lead #%s priced at $%s (%s)", lead.id, amount, source)
    return result


def price_lead_in_background(lead_id):
    """Fire-and-forget from the public quote form: the guest's submission must
    never wait on a Distance Matrix call."""
    from reservations.utils import _run_in_background

    _run_in_background(price_lead, lead_id)
