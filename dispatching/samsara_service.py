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
from decimal import Decimal

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# Samsara rejects a stats request carrying more than four types:
#   400 {"message": "Vehicle stats are currently restricted to 4 types."}
# Callers chunk on this rather than trusting the list below to stay short — a
# fifth type added later must degrade into a second request, not a 400.
MAX_STAT_TYPES_PER_REQUEST = 4

# Extended telemetry for the Fleet Management module. Requested in a SEPARATE
# call from gps (see sync_vehicles) so a bad type here can never take down live
# position tracking on the dispatch board.
#
# Measured by `manage.py fleet_probe` against the live account, 2026-08-05:
#     obdOdometerMeters   11/11   exact odometer, the mileage primary
#     gpsDistanceMeters   11/11   cumulative distance, the mileage fallback
#     batteryMilliVolts   11/11   readiness
#     faultCodes          11/11   readiness
#     fuelPercents        11/11   readiness  (response key is fuelPercent —
#                                 see _STAT_KEY_ALIASES)
#     engineStates        11/11   On / Off / Idle (response key is engineState)
#     obdEngineSeconds     5/11   engine hours; the 5 that expose the PID report
#                                 it reliably, the rest are simply absent from
#                                 the response. Display-only for now.
# Seven types means TWO requests (the cap is 4) — fine and deliberate.
#
# Deliberately NOT requested:
#     gpsOdometerMeters    —      settable/drifting; never our source of truth
#     engineRpm / defLevelMilliPercent — no operational use here
# Re-run fleet_probe before adding any back; the UI labels on the detail page
# key off this tuple, so a change here fixes the wording automatically.
EXTENDED_STAT_TYPES = (
    "obdOdometerMeters",
    "gpsDistanceMeters",
    "batteryMilliVolts",
    "faultCodes",
    "fuelPercents",
    "engineStates",
    "obdEngineSeconds",
)

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


# THE NAME TRAP. What you ASK for is not always what comes BACK.
#
# Measured against the live account 2026-08-05 by requesting each type alone and
# printing the response keys: every type echoes its own name back EXCEPT these
# two, which come back singular from /fleet/vehicles/stats. Requesting
# "fuelPercents" and then reading record["fuelPercents"] silently finds nothing,
# which reads exactly like "the plan doesn't include fuel" — it cost us that
# wrong conclusion once, while the Samsara dashboard happily showed fuel levels.
#
# The endpoints also disagree on SHAPE: /stats gives one {time, value} dict,
# while /stats/feed and /stats/history give a LIST of them. _stat_block
# normalises both, so the same parser works against all three.
_STAT_KEY_ALIASES = {
    "fuelPercents": ("fuelPercent", "fuelPercents"),
    "engineStates": ("engineState", "engineStates"),
}


def _stat_block(record, stat_type):
    """
    The latest {time, value} dict for `stat_type`, or None.

    Tolerates the singular/plural key difference and the dict-vs-list shape
    difference between the snapshot, feed and history endpoints.
    """
    for key in _STAT_KEY_ALIASES.get(stat_type, (stat_type,)):
        block = (record or {}).get(key)
        if isinstance(block, dict):
            return block
        if isinstance(block, list) and block:
            # feed/history return a series; the last entry is the newest.
            last = block[-1]
            if isinstance(last, dict):
                return last
    return None


def parse_stats_record(record):
    """
    Extended telemetry from one Samsara stats record -> FleetVehicle field values.

    Complements parse_gps_record (which owns position/movement and is unchanged).
    Kept separate because the two have different emptiness rules: a car with no
    GPS fix may still report an odometer, and vice versa.

    THE LOAD-BEARING RULE: only keys actually PRESENT in the payload are emitted.
    An unentitled stat type, or a GPS-only asset gateway, simply yields fewer
    keys — it must never produce a None that overwrites a good stored value.
    Stale-but-real beats fresh-and-null; the *_at timestamps let the UI age it.

    Pure function — unit-testable with fixture JSON and no HTTP.
    """
    record = record or {}
    out = {}

    def _block(key):
        return _stat_block(record, key)

    def _sample_time(block):
        raw = block.get("time")
        return parse_datetime(raw) if raw else None

    # --- odometer: the OBD bus only ----------------------------------------
    # gpsDistanceMeters deliberately does NOT populate this column. It is a
    # distance-since-install counter, not the vehicle's odometer; writing it here
    # would show a 3-year-old Suburban with 8,000 miles on the clock. It is
    # stored separately below and used only for day-over-day deltas.
    odo = _block("obdOdometerMeters")
    if odo and odo.get("value") is not None:
        out["samsara_odometer_meters"] = Decimal(str(odo["value"]))
        out["samsara_odometer_source"] = "obd"
        sampled = _sample_time(odo)
        if sampled:
            out["samsara_odometer_at"] = sampled

    dist = _block("gpsDistanceMeters")
    if dist and dist.get("value") is not None:
        out["samsara_gps_distance_meters"] = Decimal(str(dist["value"]))

    fuel = _block("fuelPercents")
    if fuel and fuel.get("value") is not None:
        try:
            out["samsara_fuel_percent"] = max(0, min(100, int(fuel["value"])))
        except (TypeError, ValueError):
            pass

    battery = _block("batteryMilliVolts")
    if battery and battery.get("value") is not None:
        try:
            out["samsara_battery_millivolts"] = int(battery["value"])
        except (TypeError, ValueError):
            pass

    engine = _block("engineStates")
    if engine and engine.get("value"):
        out["samsara_engine_state"] = str(engine["value"])[:16]

    hours = _block("obdEngineSeconds")
    if hours and hours.get("value") is not None:
        try:
            out["samsara_engine_seconds"] = int(hours["value"])
        except (TypeError, ValueError):
            pass

    # --- fault count -------------------------------------------------------
    # faultCodes comes back shaped by bus (obdii / j1939 / passenger). We only
    # want a COUNT on the vehicle row; the episodes themselves live in
    # VehicleFault so a code seen on 1,000 polls stays one row.
    faults = record.get("faultCodes")
    count, faults_at = _count_faults(faults)
    if count is not None:
        out["samsara_open_fault_count"] = count
        if faults_at:
            out["samsara_faults_at"] = faults_at

    return out


# DTC buckets that represent a REAL, active problem. `pendingDtcs` is excluded
# on purpose: a pending code is a single unconfirmed occurrence that the ECU has
# not yet promoted, and surfacing those would put a red badge on healthy cars.
_ACTIVE_DTC_KEYS = ("confirmedDtcs", "permanentDtcs")


def _count_faults(faults):
    """
    (count, sample_time) from a faultCodes block, or (None, None) when the type
    was not returned at all.

    Returning None for "absent" rather than 0 matters: 0 means we asked and the
    car is clean; absent means we never got an answer. Writing 0 for absent would
    silently clear a real fault badge.

    Payload shape (verified against the live account, 2026-08-05) — the trap here
    is that the two buses nest differently:

      obdii.diagnosticTroubleCodes[]  is a list of ECUs, NOT of faults. Each entry
        carries its own confirmedDtcs/pendingDtcs/permanentDtcs lists plus a
        milStatus (check-engine light). A healthy Suburban returns FOUR such
        entries, all with empty lists — counting the entries reports "4 faults"
        on a car with none.

      j1939.diagnosticTroubleCodes[]  is a list of actual faults (spn/fmi).

    So: an entry that carries a DTC bucket is a container to look inside; any
    other entry is itself a fault.
    """
    if faults is None:
        return None, None

    if isinstance(faults, list):  # already a flat list of faults
        return len(faults), None

    if not isinstance(faults, dict):
        return None, None

    sampled = parse_datetime(faults["time"]) if faults.get("time") else None

    total = 0
    answered = False
    for key, bus in faults.items():
        if key == "time" or not isinstance(bus, dict):
            continue
        entries = bus.get("diagnosticTroubleCodes")
        if not isinstance(entries, list):
            continue
        answered = True  # the bus reported, even if it reported nothing wrong

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if any(k in entry for k in _ACTIVE_DTC_KEYS):
                # obdii ECU wrapper — count the codes inside it.
                for dtc_key in _ACTIVE_DTC_KEYS:
                    codes = entry.get(dtc_key)
                    if isinstance(codes, list):
                        total += len(codes)
                # A lit check-engine lamp with no readable code is still a fault.
                if entry.get("milStatus") and total == 0:
                    total += 1
            else:
                total += 1  # j1939-style: the entry IS the fault

    return (total if answered else None), sampled


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
