import hashlib
import requests
from django.conf import settings
from django.core.cache import caches

# Use LocMemCache (default) with 2hr TTL (traffic changes throughout day)
_cache = caches["default"]
CACHE_TTL = 60 * 60 * 2  # 2 hours


def get_drive_time(origin, destination, force_refresh=False):
    """
    Call Google Distance Matrix API to get estimated drive time and distance.
    Uses departure_time=now for traffic-aware estimates.
    Returns {"duration_text": "25 mins", "distance_text": "18.3 mi"} or None.
    Results are cached for 2 hours.
    """
    api_key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
    if not api_key or not origin or not destination:
        return None

    # Cache key based on origin + destination hash
    key_raw = f"drivetime:{origin}|{destination}"
    cache_key = f"dt_{hashlib.md5(key_raw.encode()).hexdigest()}"

    if not force_refresh:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": origin,
                "destinations": destination,
                "units": "imperial",
                "departure_time": "now",
                "key": api_key,
            },
            timeout=5,
        )
        data = resp.json()

        if data.get("status") != "OK":
            return None

        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            return None

        # Prefer duration_in_traffic (traffic-aware) over plain duration
        duration = element.get("duration_in_traffic", element["duration"])

        result = {
            "duration_text": duration["text"],
            "distance_text": element["distance"]["text"],
        }
        _cache.set(cache_key, result, CACHE_TTL)
        return result

    except Exception:
        return None
