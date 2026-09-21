"""Price a "QUOTE NEEDED" task the moment it is filed.

A guest asks the website for a route the rate card cannot quote, and a manual
task is filed for a person to send a price. The price itself is not a judgement
call — the quote calculator's engine gives the same answer to everyone — so it
is worked out here, in the background, right after the task is created, and
written onto the task: the number, where it came from, the distance and drive
time, and the breakdown. The task page then opens with the text already priced,
and the queue row carries the number in its title.

Nothing here is customer-facing. `Lead.estimated_price` and `Quote.estimated_price`
are left alone on purpose: they mean "the website quoted this", and the whole
reason the task exists is that it did not.
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
