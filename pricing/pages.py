"""
Public marketing pages for the two new service lines:

  GET /fleet/                 → the fleet, presented by class
  GET /hourly-city-to-city/   → hourly charters + city-to-city, with the
                                embedded instant-quote widget

Both pages pull every price and label from the admin-editable pricing models —
nothing is hardcoded in the templates or JS. The widget receives a single JSON
blob (`widget_data`) it uses to render dropdowns and the "from $X" displays.
"""

from __future__ import annotations

from django.conf import settings
from django.shortcuts import render
from django.templatetags.static import static

from .models import CityRoute, HourlyRate, PricingConfig, VehicleClass

# Class key → fleet photo. (Display name / capacities all come from the DB.)
VEHICLE_IMAGE_MAP = {
    "towncar": "images/towncar.webp",
    "mini_van": "images/minivan.webp",
    "suv": "images/suv.webp",
    "van": "images/van.webp",
    "sprinter": "images/sprinter.webp",
}


def vehicle_image_path(vc) -> str:
    """Static-relative path for a class's photo (templates apply {% static %})."""
    return VEHICLE_IMAGE_MAP.get(vc.vehicle_type) or VEHICLE_IMAGE_MAP.get(
        vc.key, "images/towncar.webp"
    )


def vehicle_image_url(vc) -> str:
    """Fully-resolved static URL — for JSON/JS payloads (the quote cards)."""
    return static(vehicle_image_path(vc))


def _active_classes():
    return list(VehicleClass.objects.filter(is_active=True).order_by("sort_order"))


def build_widget_data() -> dict:
    """The single JSON payload the quote widget consumes. All prices are live
    from the admin config."""
    config = PricingConfig.load()
    classes = _active_classes()
    hourly = {h.vehicle_class_id: h for h in HourlyRate.objects.all()}

    class_data = []
    for vc in classes:
        h = hourly.get(vc.id)
        class_data.append(
            {
                "key": vc.key,
                "name": vc.display_name,
                "passengers": vc.passenger_capacity,
                "luggage": vc.luggage_capacity,
                "hourly_rate": float(h.hourly_rate) if h else None,
                "minimum_hours": float(h.minimum_hours) if h else None,
            }
        )

    routes = (
        CityRoute.objects.filter(is_active=True)
        .prefetch_related("prices__vehicle_class")
        .order_by("sort_order")
    )
    route_data = []
    for r in routes:
        prices = {p.vehicle_class.key: float(p.price) for p in r.prices.all()}
        route_data.append(
            {
                "id": r.id,
                "name": r.name,
                "origin": r.origin_label,
                "miles": r.approx_miles,
                "prices": prices,
            }
        )

    return {
        "classes": class_data,
        "routes": route_data,
        "gratuity_percentage": float(config.gratuity_percentage),
        "all_inclusive_note": config.all_inclusive_note,
        "hourly_tolls_note": config.hourly_tolls_note,
        "quote_url": "/api/quote/",
        "results_url": "/transfer-quote/",
    }


def fleet_page(request):
    classes = _active_classes()
    cards = [
        {
            "name": vc.display_name,
            "passengers": vc.passenger_capacity,
            "luggage": vc.luggage_capacity,
            "ideal_for": vc.ideal_for,
            "image": vehicle_image_path(vc),
        }
        for vc in classes
    ]
    return render(request, "pricing/fleet.html", {"fleet": cards})


def charters_page(request):
    config = PricingConfig.load()
    classes = _active_classes()
    hourly = {h.vehicle_class_id: h for h in HourlyRate.objects.all()}

    # Hourly "from $X/hr" rows
    hourly_rows = []
    for vc in classes:
        h = hourly.get(vc.id)
        if h:
            hourly_rows.append(
                {
                    "name": vc.display_name,
                    "rate": h.hourly_rate,
                    "minimum_hours": h.minimum_hours,
                    "peak_minimum_hours": h.peak_minimum_hours,
                }
            )

    # City-to-city route table (lowest price per route = the "from" figure)
    routes = (
        CityRoute.objects.filter(is_active=True)
        .prefetch_related("prices__vehicle_class")
        .order_by("sort_order")
    )
    route_rows = []
    for r in routes:
        prices = {p.vehicle_class.key: p.price for p in r.prices.all()}
        from_price = min(prices.values()) if prices else None
        route_rows.append(
            {
                "name": r.name,
                "miles": r.approx_miles,
                "from_price": from_price,
                "prices": [prices.get(vc.key) for vc in classes],
            }
        )

    context = {
        "config": config,
        "classes": classes,
        "hourly_rows": hourly_rows,
        "route_rows": route_rows,
        "widget_data": build_widget_data(),
        "maps_browser_key": settings.GOOGLE_MAPS_BROWSER_KEY,
    }
    return render(request, "pricing/charters.html", context)
