"""
Thin wrapper around the Samsara Cloud API.

Read-only for now. We only ever GET; never POST/PUT/DELETE. If we eventually
need writes (geofences, route sync), add explicit write methods alongside —
don't add a generic _post.
"""

import logging
import time

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


class SamsaraError(Exception):
    """Base for all Samsara client errors."""


class SamsaraAuthError(SamsaraError):
    """401/403 — bad token or missing scope."""


class SamsaraRateLimitError(SamsaraError):
    """429 — backoff and retry later."""


class SamsaraAPIError(SamsaraError):
    """5xx or unexpected status."""


class SamsaraClient:
    def __init__(self, api_key=None, base_url=None, timeout=10):
        self.api_key = api_key or settings.SAMSARA_API_KEY
        self.base_url = (base_url or settings.SAMSARA_API_BASE_URL).rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"
        self.session.headers["Accept"] = "application/json"

        if not self.api_key:
            # We allow construction without a key so admin UI doesn't crash,
            # but any call will raise SamsaraAuthError.
            logger.warning("SamsaraClient constructed without an API key")

    # ---------------- internal ----------------

    def _get(self, path, params=None, _retry=True):
        if not self.api_key:
            raise SamsaraAuthError("SAMSARA_API_KEY is not set")

        url = f"{self.base_url}{path}"
        logger.debug("samsara GET %s params=%s", path, params)

        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise SamsaraAPIError(f"network error: {e}") from e

        if resp.status_code in (401, 403):
            raise SamsaraAuthError(f"{resp.status_code} from {path}: {resp.text[:200]}")
        if resp.status_code == 429:
            raise SamsaraRateLimitError(f"rate-limited on {path}")
        if 500 <= resp.status_code < 600:
            if _retry:
                time.sleep(1.0)
                return self._get(path, params=params, _retry=False)
            raise SamsaraAPIError(f"{resp.status_code} from {path}: {resp.text[:200]}")
        if not resp.ok:
            raise SamsaraAPIError(f"{resp.status_code} from {path}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as e:
            raise SamsaraAPIError(f"non-JSON response from {path}: {e}") from e

    def _get_paginated(self, path, params=None, items_key="data"):
        """Walk Samsara cursor pagination. Returns the flattened list."""
        params = dict(params or {})
        out = []
        cursor = None
        while True:
            if cursor:
                params["after"] = cursor
            payload = self._get(path, params=params)
            items = payload.get(items_key) or []
            out.extend(items)
            pagination = payload.get("pagination") or {}
            if pagination.get("hasNextPage") and pagination.get("endCursor"):
                cursor = pagination["endCursor"]
                continue
            break
        return out

    # ---------------- public ----------------

    def get_org(self):
        """GET /me — cheapest endpoint, used for auth check."""
        return self._get("/me")

    def list_vehicles(self):
        """GET /fleet/vehicles — all vehicles in the org."""
        return self._get_paginated("/fleet/vehicles")

    def get_vehicle_stats(self, vehicle_ids, types):
        """
        GET /fleet/vehicles/stats — point-in-time stats for the listed vehicles.
        types: list of stat types like ['gps', 'fuelPercents', 'obdOdometerMeters', 'engineStates']
        """
        if not vehicle_ids:
            return []
        params = {
            "vehicleIds": ",".join(str(v) for v in vehicle_ids),
            "types": ",".join(types),
        }
        return self._get_paginated("/fleet/vehicles/stats", params=params)

    def list_maintenance_issues(self):
        """GET /fleet/maintenance/list — active maintenance issues across the fleet."""
        return self._get_paginated("/fleet/maintenance/list")
