"""
Samsara fleet-telematics service (Phase 1: read-only live vehicle visibility).

Mirrors the shape of dispatching/aeroapi_service.py:
  - a single class holding a reused requests.Session with the auth header set once
  - every method returns a dict with a "status" key
    (success / not_found / rate_limited / error) and NEVER raises to the caller

Everything is gated behind settings.SAMSARA_API_TOKEN. When the token is empty
the service reports is_configured() == False and callers short-circuit, so the
whole integration is inert until the token lands in the environment.

Docs reference (confirm against current Samsara API if fields drift):
  GET /fleet/vehicles/stats?types=gps -> data[].gps.{latitude,longitude,time,
      speedMilesPerHour, reverseGeo.formattedLocation}, pagination.endCursor
  GET /fleet/vehicles                 -> data[].{id,name,vin,licensePlate}
"""
import logging
import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# Below this speed (mph) we treat the vehicle as not moving. Small floor avoids
# GPS jitter reading as "driving" while parked.
_MOVING_SPEED_MPH = 1.0

# Safety cap so a bad cursor / huge fleet can never spin the poller forever.
_MAX_PAGES = 25


class SamsaraService:
    """Thin client for the Samsara fleet API. Never raises; returns status dicts."""

    def __init__(self):
        self.api_token = getattr(settings, "SAMSARA_API_TOKEN", "") or ""
        self.base_url = getattr(settings, "SAMSARA_BASE_URL", "https://api.samsara.com").rstrip("/")
        self.session = requests.Session()
        if self.api_token:
            self.session.headers.update({
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json",
            })

    def is_configured(self) -> bool:
        """True only when a Samsara token is present. The inert-gate every caller checks first."""
        return bool(self.api_token)

    # --- internal HTTP with AeroAPI-style error handling -----------------

    def _get_paginated(self, path, params=None):
        """
        GET a Samsara list endpoint, following the cursor pagination, and return
        the accumulated `data` list. Returns an AeroAPI-style status dict:
          {"status": "success", "data": [...]}
          {"status": "rate_limited", "retry_after": int}
          {"status": "not_found"}
          {"status": "error", "error": str}
        """
        if not self.is_configured():
            return {"status": "error", "error": "Samsara token not configured"}

        params = dict(params or {})
        url = f"{self.base_url}{path}"
        collected = []
        pages = 0
        try:
            while True:
                pages += 1
                response = self.session.get(url, params=params, timeout=10)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", "60"))
                    return {
                        "status": "rate_limited",
                        "retry_after": retry_after,
                        "error": f"Rate limited; retry after {retry_after}s",
                    }
                if response.status_code == 404:
                    return {"status": "not_found", "error": f"Not found: {path}"}
                if response.status_code in (401, 403):
                    return {
                        "status": "error",
                        "error": f"Auth failed ({response.status_code}) — check SAMSARA_API_TOKEN / plan entitlements",
                    }

                response.raise_for_status()
                payload = response.json()
                collected.extend(payload.get("data", []) or [])

                pagination = payload.get("pagination") or {}
                cursor = pagination.get("endCursor")
                if not pagination.get("hasNextPage") or not cursor or pages >= _MAX_PAGES:
                    break
                params["after"] = cursor

            return {"status": "success", "data": collected}

        except requests.RequestException as e:
            logger.warning(f"Samsara request failed for {path}: {e}")
            return {"status": "error", "error": str(e)}
        except (ValueError, KeyError) as e:  # bad JSON / unexpected shape
            logger.warning(f"Samsara response parse failed for {path}: {e}")
            return {"status": "error", "error": str(e)}

    # --- public methods --------------------------------------------------

    def get_vehicle_stats(self, vehicle_ids=None, types=("gps",)):
        """
        Current stats for the fleet. `types` defaults to GPS only (Phase 1).
        Optionally filter to specific Samsara vehicle ids.
        """
        params = {"types": ",".join(types)}
        if vehicle_ids:
            params["vehicleIds"] = ",".join(str(v) for v in vehicle_ids)
        return self._get_paginated("/fleet/vehicles/stats", params)

    def list_vehicles(self):
        """All vehicles in the Samsara account. Used by the mapping helper command."""
        return self._get_paginated("/fleet/vehicles", {})


def parse_gps_record(record):
    """
    Turn one Samsara stats record into the FleetVehicle.samsara_* field values.

    Returns a dict of field -> value, or None when the record carries no usable
    GPS fix (so the poller leaves the vehicle's last-known position untouched).
    Pure function — unit-testable without any HTTP.
    """
    gps = (record or {}).get("gps") or {}
    lat = gps.get("latitude")
    lng = gps.get("longitude")
    if lat is None or lng is None:
        return None

    sample_time = parse_datetime(gps.get("time")) if gps.get("time") else None
    speed = gps.get("speedMilesPerHour")
    movement = ""
    if speed is not None:
        movement = "driving" if speed > _MOVING_SPEED_MPH else "idle"
    label = ((gps.get("reverseGeo") or {}).get("formattedLocation") or "")[:128]

    return {
        "samsara_last_latitude": lat,
        "samsara_last_longitude": lng,
        "samsara_last_location_label": label,
        "samsara_movement_status": movement,
        "samsara_last_seen_at": sample_time,
    }


def resolve_assigned_fleet_vehicle(leg):
    """
    The FleetVehicle a leg's driver is in on the leg's pickup date, via the
    per-day DriverVehicleAssignment. Returns None when there's no driver, no
    assignment, or no vehicle on the assignment. Shared by the dispatch views
    (Phase 1) and reused verbatim by the Phase 2 risk engine.
    """
    from drivers.models import DriverVehicleAssignment

    if not getattr(leg, "driver_id", None) or not leg.pickup_date:
        return None
    assignment = (
        DriverVehicleAssignment.objects
        .filter(driver_id=leg.driver_id, date=leg.pickup_date)
        .select_related("vehicle")
        .first()
    )
    return assignment.vehicle if assignment else None
