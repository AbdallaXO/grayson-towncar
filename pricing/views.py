"""
Public endpoints for the quote engine:

  POST /api/quote/            → instant quote (JSON), returns a hand-off token
  GET/POST /book-quote/<token>/ → convert a quote into a real reservation and
                                  hand off to the existing Stripe checkout

The booking step reuses the existing reservation/checkout flow; it never
re-prices (it reads the stored InstantQuote) so the customer can't be charged a
different number than they were quoted.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from rates.models import Vehicle
from reservations.forms import CustomerForm, LegForm
from reservations.models import Leg, Reservation

from .models import InstantQuote, PricingConfig, VehicleClass
from .pages import vehicle_image_path, vehicle_image_url
from .services import QuoteError, compute_quote, quote_all_classes

logger = logging.getLogger(__name__)

QUOTE_TTL_DAYS = 14


def _fmt_hours(value: Decimal | None):
    if value is None:
        return None
    value = Decimal(value)
    # 3.0 -> "3", 3.5 -> "3.5"
    return f"{value.normalize():f}" if value == value.to_integral() else f"{value.normalize():f}"


def _serialize(quote: InstantQuote, result) -> dict:
    """Build the JSON payload the widget renders."""
    money = lambda v: None if v is None else float(v)  # noqa: E731
    return {
        "ok": True,
        "token": str(quote.token),
        "book_url": f"/book-quote/{quote.token}/",
        "currency": "USD",
        "service_type": result.service_type,
        "vehicle_class": {
            "key": result.vehicle_class.key,
            "name": result.vehicle_class.display_name,
        },
        "date": result.service_date.isoformat(),
        "base_price": money(result.base_price),
        "peak_adjustment": money(result.peak_adjustment),
        "gratuity": money(result.gratuity),
        "total": money(result.total),
        "all_inclusive": result.all_inclusive,
        "price_source": result.price_source,
        "trip": {
            "origin": result.origin,
            "destination": result.destination,
            "hours": _fmt_hours(result.hours),
            "minimum_hours": _fmt_hours(result.minimum_hours),
            "loaded_miles": money(result.loaded_miles),
        },
        "notes": result.notes,
    }


def _serialize_options(quotes, service_type, service_date, hours) -> dict:
    """Build the all-classes preview payload (one card per active class). No
    token here — the widget locks a quote by re-calling /api/quote/ with the
    chosen class once the customer selects one."""
    config = PricingConfig.load()
    money = lambda v: None if v is None else float(v)  # noqa: E731

    # Every available class shares the same trip; read it off the first one.
    trip = {"origin": "", "destination": "", "loaded_miles": None, "hours": _fmt_hours(hours)}
    for cq in quotes:
        if cq.available and cq.result:
            r = cq.result
            trip = {
                "origin": r.origin,
                "destination": r.destination,
                "loaded_miles": money(r.loaded_miles),
                "hours": _fmt_hours(r.hours),
            }
            break

    options = []
    for cq in quotes:
        vc = cq.vehicle_class
        opt = {
            "vehicle_class": {
                "key": vc.key,
                "name": vc.display_name,
                "passengers": vc.passenger_capacity,
                "luggage": vc.luggage_capacity,
                "ideal_for": vc.ideal_for,
                "image": vehicle_image_url(vc),
            },
            "available": cq.available,
        }
        if cq.available and cq.result:
            r = cq.result
            opt.update(
                {
                    "base_price": money(r.base_price),
                    "peak_adjustment": money(r.peak_adjustment),
                    "gratuity": money(r.gratuity),
                    "total": money(r.total),
                    "all_inclusive": r.all_inclusive,
                    "price_source": r.price_source,
                    "notes": r.notes,
                }
            )
        else:
            opt["unavailable_reason"] = cq.unavailable_reason
        options.append(opt)

    return {
        "ok": True,
        "mode": "options",
        "currency": "USD",
        "service_type": service_type,
        "date": service_date.isoformat(),
        "gratuity_percentage": float(config.gratuity_percentage),
        "trip": trip,
        "options": options,
    }


def _parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@require_POST
def quote_api(request):
    """Compute an instant quote and persist a hand-off token."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = request.POST

    service_type = (payload.get("service_type") or "").strip()
    vehicle_class_key = (payload.get("vehicle_class") or "").strip()
    service_date = _parse_date(payload.get("date"))
    origin = (payload.get("origin") or "").strip()
    destination = (payload.get("destination") or "").strip()
    route_id = payload.get("route_id") or None

    hours = None
    if payload.get("hours") not in (None, ""):
        try:
            hours = Decimal(str(payload.get("hours")))
        except (InvalidOperation, ValueError):
            return _error("Please enter a valid number of hours.", "bad_hours", "hours")

    # No vehicle class → Blacklane-style preview: price every active class for
    # the trip in one shot (one distance lookup). No token is minted; the widget
    # re-calls this endpoint WITH a class on "Select" to lock the quote.
    if not vehicle_class_key:
        try:
            quotes = quote_all_classes(
                service_type=service_type,
                service_date=service_date,
                origin=origin,
                destination=destination,
                route_id=int(route_id) if route_id else None,
                hours=hours,
            )
        except QuoteError as exc:
            return _error(exc.message, exc.code, exc.field)
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error computing all-class quote")
            return _error("We couldn't generate that quote. Please call (407) 212-7190.", "server_error")
        return JsonResponse(_serialize_options(quotes, service_type, service_date, hours))

    try:
        result = compute_quote(
            service_type=service_type,
            vehicle_class_key=vehicle_class_key,
            service_date=service_date,
            origin=origin,
            destination=destination,
            route_id=int(route_id) if route_id else None,
            hours=hours,
        )
    except QuoteError as exc:
        return _error(exc.message, exc.code, exc.field)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error computing quote")
        return _error("We couldn't generate that quote. Please call (407) 212-7190.", "server_error")

    quote = _persist_quote(result)
    return JsonResponse(_serialize(quote, result))


def _persist_quote(result) -> InstantQuote:
    """Store a computed quote so the booking step reads it back instead of
    re-pricing (the customer can never be charged a different number)."""
    return InstantQuote.objects.create(
        service_type=result.service_type,
        vehicle_class=result.vehicle_class,
        service_date=result.service_date,
        origin=result.origin,
        destination=result.destination,
        city_route=result.city_route,
        loaded_miles=result.loaded_miles,
        hours=result.hours,
        base_price=result.base_price,
        peak_adjustment=result.peak_adjustment,
        gratuity=result.gratuity,
        total=result.total,
        all_inclusive=result.all_inclusive,
        price_source=result.price_source,
        expires_at=timezone.now() + timedelta(days=QUOTE_TTL_DAYS),
    )


def _error(message, code="invalid", field=None, status=400):
    return JsonResponse(
        {"ok": False, "error": {"message": message, "code": code, "field": field}},
        status=status,
    )


def book_quote(request, token):
    """Render the streamlined contact form for a stored quote and, on submit,
    create a rate-less Reservation and hand off to checkout."""
    quote = get_object_or_404(InstantQuote, token=token)

    if quote.is_expired:
        return render(request, "pricing/quote_expired.html", {"quote": quote}, status=410)

    config = PricingConfig.load()
    prefill_pickup = quote.origin if quote.service_type == "city_to_city" else ""
    prefill_dropoff = (
        quote.destination if quote.service_type == "city_to_city" else "As directed (hourly charter)"
    )

    if request.method == "POST":
        customer_form = CustomerForm(request.POST)
        leg_form = LegForm(request.POST, route=None)
        if customer_form.is_valid() and leg_form.is_valid():
            customer = customer_form.save()

            try:
                passenger_count = max(1, int(request.POST.get("passenger_count", 1)))
            except (TypeError, ValueError):
                passenger_count = 1

            vehicle = Vehicle.objects.filter(
                vehicle_type=quote.vehicle_class.vehicle_type
            ).first()

            reservation = Reservation(
                trip_type="one_way",
                service_type=quote.service_type,
                customer=customer,
                rate=None,
                vehicle=vehicle,
                quoted_hours=quote.hours,
                passenger_count=passenger_count,
                base_price=quote.effective_base,
                additional_charges=Decimal("0.00"),
                gratuity_percentage=config.gratuity_percentage,
                gratuity_amount=quote.gratuity,
                total_price=quote.total,
                status="confirmed",
            )
            reservation.save()

            # Capture attribution from cookies (same convention as the legacy flow).
            for param in ["gclid", "fbclid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]:
                value = request.POST.get(param) or request.COOKIES.get(param)
                if value:
                    setattr(reservation, param, value)
            reservation.save()

            leg = leg_form.save(commit=False)
            leg.reservation = reservation
            leg.save()

            quote.converted_reservation = reservation
            quote.save(update_fields=["converted_reservation"])

            logger.info(
                "Quote %s converted to reservation %s (%s)",
                quote.token, reservation.id, quote.service_type,
            )
            return redirect("create_checkout_session", reservation_id=reservation.uuid)
    else:
        customer_form = CustomerForm()
        leg_form = LegForm(
            initial={
                "pickup_date": quote.service_date,
                "pickup_location": prefill_pickup,
                "dropoff_location": prefill_dropoff,
            },
            route=None,
        )

    context = {
        "quote": quote,
        "customer_form": customer_form,
        "leg_form": leg_form,
        "config": config,
        "is_hourly": quote.service_type == "hourly",
        "prefill_pickup": prefill_pickup,
        "prefill_dropoff": prefill_dropoff,
    }
    return render(request, "pricing/book_quote.html", context)


# ---------------------------------------------------------------------------
# Two-step quote: full-width "choose your experience" results page
# ---------------------------------------------------------------------------
def _options_for_template(quotes) -> list[dict]:
    """Shape a ClassQuote list into plain dicts the results template renders."""
    out = []
    for cq in quotes:
        vc = cq.vehicle_class
        item = {
            "key": vc.key,
            "name": vc.display_name,
            "passengers": vc.passenger_capacity,
            "luggage": vc.luggage_capacity,
            "ideal_for": vc.ideal_for,
            "image": vehicle_image_path(vc),  # template applies {% static %}
            "available": cq.available,
        }
        if cq.available and cq.result:
            r = cq.result
            item.update(
                total=r.total,
                gratuity=r.gratuity,
                all_inclusive=r.all_inclusive,
                peak_adjustment=r.peak_adjustment,
                price_source=r.price_source,
            )
        else:
            item["unavailable_reason"] = cq.unavailable_reason
        out.append(item)
    return out


def quote_results(request):
    """Full-width results page: price every class for the trip, show the route
    map, and let the customer Select a class (which locks the quote → checkout).
    Server-rendered so the cards work without JS; the map is a JS enhancement."""
    service_type = (request.GET.get("service_type") or "city_to_city").strip()
    origin = (request.GET.get("origin") or "").strip()
    destination = (request.GET.get("destination") or "").strip()
    service_date = _parse_date(request.GET.get("date"))

    hours = None
    if request.GET.get("hours"):
        try:
            hours = Decimal(str(request.GET.get("hours")))
        except (InvalidOperation, ValueError):
            hours = None

    config = PricingConfig.load()
    options, error = [], None
    trip = {"origin": origin, "destination": destination, "loaded_miles": None, "hours": hours}

    try:
        quotes = quote_all_classes(
            service_type=service_type,
            service_date=service_date,
            origin=origin,
            destination=destination,
            hours=hours,
        )
        options = _options_for_template(quotes)
        for cq in quotes:
            if cq.available and cq.result:
                r = cq.result
                trip = {
                    "origin": r.origin,
                    "destination": r.destination,
                    "loaded_miles": r.loaded_miles,
                    "hours": r.hours,
                }
                break
    except QuoteError as exc:
        error = exc.message

    any_peak = any(o.get("peak_adjustment") for o in options)

    context = {
        "service_type": service_type,
        "origin": origin,
        "destination": destination,
        "date": service_date,
        "date_str": service_date.isoformat() if service_date else "",
        "hours": hours,
        "options": options,
        "trip": trip,
        "any_peak": any_peak,
        "error": error,
        "config": config,
        "maps_browser_key": settings.GOOGLE_MAPS_BROWSER_KEY,
    }
    return render(request, "pricing/quote_results.html", context)


@require_POST
def select_quote(request):
    """Lock a chosen class from the results page: mint the InstantQuote and hand
    off to the booking page. A server-side form post — no JS required to book."""
    service_type = (request.POST.get("service_type") or "").strip()
    vehicle_class_key = (request.POST.get("vehicle_class") or "").strip()
    service_date = _parse_date(request.POST.get("date"))
    origin = (request.POST.get("origin") or "").strip()
    destination = (request.POST.get("destination") or "").strip()

    hours = None
    if request.POST.get("hours"):
        try:
            hours = Decimal(str(request.POST.get("hours")))
        except (InvalidOperation, ValueError):
            hours = None

    try:
        result = compute_quote(
            service_type=service_type,
            vehicle_class_key=vehicle_class_key,
            service_date=service_date,
            origin=origin,
            destination=destination,
            hours=hours,
        )
    except QuoteError:
        # Bounce back to the results page, which re-prices and surfaces the issue.
        params = {"service_type": service_type, "date": request.POST.get("date") or ""}
        if service_type == "hourly":
            params["hours"] = request.POST.get("hours") or ""
        else:
            params["origin"], params["destination"] = origin, destination
        return redirect(f"{reverse('quote_results')}?{urlencode(params)}")

    quote = _persist_quote(result)
    return redirect("book_quote", token=quote.token)
