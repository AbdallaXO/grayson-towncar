"""
Loaded-mileage lookup for city-to-city quotes on UNLISTED routes.

Cost discipline (we have had Distance Matrix billing spikes):
  1. Named CityRoutes never call this module at all — they read the route table.
  2. Unlisted routes first hit RouteDistanceCache (a precomputed miles table).
  3. Only on a cache miss do we call Google Distance Matrix — once — and then
     write the result back to the cache so it is never billed for that pair
     again. Every live call logs the greppable tag GTC-GOOGLE-LIVE-DISTANCE.

A live call can be disabled entirely with PRICING_ALLOW_LIVE_DISTANCE=0, in
which case an uncached unlisted route raises DistanceUnavailable and the quote
endpoint asks the customer to call for a price (no silent billing).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

import requests
from django.conf import settings

from .models import RouteDistanceCache

logger = logging.getLogger(__name__)


class DistanceUnavailable(Exception):
    """Raised when miles cannot be resolved without a live call that is not
    allowed (or the live call failed)."""


def normalize_place(text: str) -> str:
    """Collapse a free-text place into a stable cache key (lowercased,
    punctuation stripped, whitespace squeezed)."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _live_allowed() -> bool:
    # Default ON in production (so a genuinely new route still quotes), but the
    # result is always cached so it bills at most once per pair.
    return getattr(settings, "PRICING_ALLOW_LIVE_DISTANCE", True) and bool(
        getattr(settings, "GOOGLE_MAPS_API_KEY", "")
    )


def get_loaded_miles(origin: str, destination: str) -> tuple[Decimal, str]:
    """
    Return (miles, source) for an unlisted route.

    source is one of the RouteDistanceCache source values ('manual', 'seed',
    'distance_matrix'). Raises DistanceUnavailable if the pair is uncached and a
    live lookup is not possible.
    """
    o_key, d_key = normalize_place(origin), normalize_place(destination)
    if not o_key or not d_key:
        raise DistanceUnavailable("Origin and destination are both required.")

    cached = RouteDistanceCache.objects.filter(
        origin_key=o_key, destination_key=d_key
    ).first()
    if cached:
        return cached.miles, cached.source

    # Symmetric fallback: a reversed pair is a fine mileage estimate.
    reverse = RouteDistanceCache.objects.filter(
        origin_key=d_key, destination_key=o_key
    ).first()
    if reverse:
        return reverse.miles, reverse.source

    if not _live_allowed():
        raise DistanceUnavailable(
            "No cached distance for this route and live lookup is disabled."
        )

    miles = _call_distance_matrix(origin, destination)
    if miles is None:
        raise DistanceUnavailable("Distance Matrix lookup failed.")

    RouteDistanceCache.objects.create(
        origin_key=o_key,
        destination_key=d_key,
        origin_text=origin[:200],
        destination_text=destination[:200],
        miles=miles,
        source="distance_matrix",
    )
    return miles, "distance_matrix"


def _call_distance_matrix(origin: str, destination: str) -> Decimal | None:
    """One static (non-traffic) Distance Matrix call returning one-way miles."""
    api_key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return None
    logger.warning(
        "GTC-GOOGLE-LIVE-DISTANCE pricing miles lookup (cache miss): %s -> %s",
        origin,
        destination,
    )
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": origin,
                "destinations": destination,
                "units": "imperial",
                "key": api_key,
            },
            timeout=5,
        )
        data = resp.json()
        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            logger.error("Distance Matrix element status: %s", element.get("status"))
            return None
        meters = element["distance"]["value"]
        miles = Decimal(meters) / Decimal("1609.344")
        return miles.quantize(Decimal("0.01"))
    except Exception as exc:  # noqa: BLE001 — never let pricing crash on the API
        logger.error("Distance Matrix call failed: %s", exc)
        return None
