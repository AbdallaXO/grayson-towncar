"""
Throwaway discovery tool: find out what this Samsara account ACTUALLY returns.

    python manage.py fleet_probe
    python manage.py fleet_probe --raw          # dump one full stats record per type
    python manage.py fleet_probe --vehicles 3   # limit how many vehicles are probed

Why this exists
---------------
The Fleet Management design has three unknowns that are cheaper to measure than
to reason about, and every one of them changes the schema:

  1. Which stat types is this plan entitled to? (odometer / fuel / battery /
     engine hours / fault codes are all plan- AND gateway-dependent.)
  2. Which of OUR vehicles actually report each type? A VG-series gateway wired
     to OBD-II returns odometer; an AG-series asset gateway returns GPS only.
     This fleet is mixed light-duty, so installs likely differ car to car.
  3. Is there a cursor-resumable stats FEED endpoint on this account, or only
     the point-in-time snapshot we use today? A persisted cursor is meaningless
     against the snapshot endpoint (its endCursor is intra-response pagination
     over the vehicle list, not a resume token).

Read-only. Issues GETs only, never writes, never raises, never prints the token.
Delete this command once the answers are recorded in docs/.
"""
import json

from django.core.management.base import BaseCommand

from drivers.models import FleetVehicle
from dispatching.samsara_service import SamsaraService


# Every stat type worth asking about for a light-duty black-car fleet. Probed ONE
# AT A TIME on purpose: if an unentitled type errors the whole response, a batched
# request would tell us nothing about which type was the problem.
CANDIDATE_TYPES = [
    ("gps", "position, speed, reverse-geo — the one we already use"),
    ("obdOdometerMeters", "PREFERRED mileage source"),
    ("gpsOdometerMeters", "explicitly NOT our source of truth (drifts/settable)"),
    ("gpsDistanceMeters", "cumulative GPS distance — mileage FALLBACK"),
    ("engineStates", "Running / Idle / Off"),
    ("fuelPercents", "tank level — readiness check"),
    ("obdEngineSeconds", "engine hours (often heavy-duty J1939 only)"),
    ("batteryMilliVolts", "battery voltage — readiness check"),
    ("faultCodes", "active DTCs"),
    ("engineRpm", "diagnostic only, not planned for use"),
    ("defLevelMilliPercent", "diesel exhaust fluid (Sprinters, maybe)"),
]

# Endpoints to existence-check. Path -> what it would unlock.
CANDIDATE_ENDPOINTS = [
    ("/me", "cheapest auth check; also names the org"),
    ("/fleet/vehicles", "vehicle master: vin, licensePlate, name"),
    ("/fleet/vehicles/stats", "point-in-time snapshot (in use today)"),
    ("/fleet/vehicles/stats/feed", "cursor-resumable delta feed (would justify a stored cursor)"),
    ("/fleet/vehicles/stats/history", "historical window — would enable backfill"),
    ("/fleet/maintenance/list", "maintenance issues / faults (path used by the abandoned branch)"),
    ("/fleet/maintenance/dvirs", "DVIR submissions"),
    ("/fleet/defects", "DVIR defects"),
    ("/fleet/vehicles/faults", "fault codes, if not on the stats endpoint"),
]

# Samsara answers 401 for a path that EXISTS but rejects the token, and a plain-text
# 404 for a path that doesn't route at all. That means an invalid token still tells
# us which endpoints are real — worth reporting even when nothing else works.
_AUTH_CODES = (401, 403)

_OK = "OK"
_NO = "--"


class Command(BaseCommand):
    help = "Probe the Samsara account for entitled stat types and endpoints. Read-only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--raw", action="store_true",
            help="Print one full JSON record per stat type that returned data.",
        )
        parser.add_argument(
            "--vehicles", type=int, default=0,
            help="Probe only the first N mapped vehicles (default: all).",
        )

    def handle(self, *args, **options):
        service = SamsaraService()
        if not service.is_configured():
            self.stdout.write(self.style.ERROR(
                "SAMSARA_API_TOKEN is not set — nothing to probe.\n"
                "Check the name in .env (it was SAMSARA_API_KEY, which settings.py "
                "does not read) and confirm the value is set on Railway."
            ))
            return

        mapped = list(
            FleetVehicle.objects.exclude(samsara_vehicle_id="")
            .order_by("vehicle_number")
        )
        if not mapped:
            self.stdout.write(self.style.WARNING(
                "No FleetVehicle has a samsara_vehicle_id — nothing to probe. "
                "Map at least one in the Django admin first."
            ))
            return

        limit = options["vehicles"]
        probed = mapped[:limit] if limit else mapped
        ids = [v.samsara_vehicle_id for v in probed]
        by_id = {v.samsara_vehicle_id: v for v in probed}

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nProbing {len(probed)} mapped vehicle(s) of {len(mapped)} total.\n"
        ))

        codes = self._probe_endpoints(service)

        # If nothing authenticated, stop here. Continuing would print a stat-type
        # table full of "no data" and a schema verdict that reads like "your plan
        # lacks odometer" when the truth is "we never got to ask". A probe that
        # lies is worse than no probe.
        # NB: 404s are excluded from the judgement — a path that doesn't route
        # says nothing about whether the token is good.
        if not any(c == 200 for c in codes.values()) and any(
            c in _AUTH_CODES for c in codes.values()
        ):
            self.stdout.write(self.style.ERROR(
                "AUTH FAILED ON EVERY ENDPOINT — the token is present but rejected.\n"
            ))
            self.stdout.write(
                "  The env var name is now correct (settings reads SAMSARA_API_TOKEN and\n"
                "  found a value), so this is the token VALUE: expired, revoked, or from\n"
                "  the wrong Samsara org. Regenerate it in the Samsara dashboard under\n"
                "  Settings -> API Tokens, then set it in BOTH .env and Railway.\n\n"
                "  Nothing below this line can be determined until that is fixed. The\n"
                "  endpoint table above is still meaningful: Samsara returns 401 for a\n"
                "  path that exists and a plain 404 for one that does not.\n"
            )
            return

        self._probe_vehicle_master(service, by_id)
        self._probe_stat_types(service, ids, by_id, raw=options["raw"])

    # ------------------------------------------------------------------
    # 1. Which endpoints exist / are entitled?
    # ------------------------------------------------------------------
    def _probe_endpoints(self, service):
        """Existence-check each candidate path. Returns {path: status_code|None}."""
        self.stdout.write(self.style.MIGRATE_HEADING("ENDPOINTS"))
        self.stdout.write(f"  {'path':<34} {'verdict':<16} note")
        self.stdout.write(f"  {'-' * 34} {'-' * 16} {'-' * 44}")

        codes = {}
        for path, purpose in CANDIDATE_ENDPOINTS:
            code, detail = self._raw_status(service, path)
            codes[path] = code

            if code == 200:
                verdict, style = "OK", self.style.SUCCESS
            elif code in _AUTH_CODES:
                # The path routed — auth is the only thing standing in the way.
                verdict, style = "exists (401)", self.style.WARNING
            elif code == 404:
                verdict, style = "NO SUCH PATH", self.style.ERROR
            else:
                verdict, style = str(code or "ERROR"), self.style.ERROR

            self.stdout.write(f"  {path:<34} {style(f'{verdict:<16}')} {purpose}")
            if detail and code not in _AUTH_CODES:
                self.stdout.write(f"  {'':<34} {'':<16} -> {detail}")

        self.stdout.write("")
        return codes

    def _raw_status(self, service, path):
        """
        HEAD-ish existence check via a 1-item GET. Returns (status_code, detail).
        A 200 means entitled; 404 means the endpoint isn't on this API version;
        401/403 means the plan or token scope doesn't cover it.
        """
        try:
            resp = service.session.get(
                f"{service.base_url}{path}", params={"limit": 1}, timeout=10
            )
        except Exception as e:  # network — never raise out of a probe
            return None, str(e)[:90]

        detail = ""
        if resp.status_code == 200:
            try:
                payload = resp.json()
                keys = list(payload.keys())
                pagination = payload.get("pagination") or {}
                detail = f"keys={keys}"
                if pagination:
                    detail += (
                        f" pagination={{hasNextPage: {pagination.get('hasNextPage')}, "
                        f"endCursor: {'present' if pagination.get('endCursor') else 'absent'}}}"
                    )
            except ValueError:
                detail = "200 but non-JSON body"
        else:
            detail = resp.text[:90].replace("\n", " ")
        return resp.status_code, detail

    # ------------------------------------------------------------------
    # 2. What identity fields does the vehicle master carry?
    # ------------------------------------------------------------------
    def _probe_vehicle_master(self, service, by_id):
        self.stdout.write(self.style.MIGRATE_HEADING("VEHICLE MASTER (/fleet/vehicles)"))
        result = service.list_vehicles()
        if result.get("status") != "success":
            self.stdout.write(self.style.ERROR(
                f"  could not list vehicles: {result.get('status')} ({result.get('error')})\n"
            ))
            return

        vehicles = result.get("data", [])
        if not vehicles:
            self.stdout.write("  (none returned)\n")
            return

        all_keys = sorted({k for v in vehicles for k in v.keys()})
        self.stdout.write(f"  {len(vehicles)} vehicle(s); fields present: {all_keys}")
        self.stdout.write("")
        self.stdout.write(f"  {'samsara id':<20} {'name':<14} {'vin':<19} {'plate':<10} mapped to")
        self.stdout.write(f"  {'-' * 20} {'-' * 14} {'-' * 19} {'-' * 10} {'-' * 20}")
        for sv in vehicles:
            sid = str(sv.get("id", ""))
            ours = by_id.get(sid)
            mapped_to = f"#{ours.vehicle_number}" if ours else self.style.WARNING("(unmapped)")
            self.stdout.write(
                f"  {sid:<20} {str(sv.get('name', ''))[:14]:<14} "
                f"{str(sv.get('vin', '') or '-'):<19} "
                f"{str(sv.get('licensePlate', '') or '-'):<10} {mapped_to}"
            )

        with_vin = sum(1 for v in vehicles if v.get("vin"))
        with_plate = sum(1 for v in vehicles if v.get("licensePlate"))
        self.stdout.write("")
        self.stdout.write(
            f"  VIN present on {with_vin}/{len(vehicles)}; "
            f"plate present on {with_plate}/{len(vehicles)} "
            f"(both are needed for auto-mapping and gateway-swap detection)"
        )
        self.stdout.write("")

    # ------------------------------------------------------------------
    # 3. Which stat types return data, for which vehicles?
    # ------------------------------------------------------------------
    def _probe_stat_types(self, service, ids, by_id, raw=False):
        self.stdout.write(self.style.MIGRATE_HEADING("STAT TYPES (/fleet/vehicles/stats)"))
        self.stdout.write(
            "  Each type requested ALONE, so one unentitled type can't mask the others.\n"
        )
        self.stdout.write(f"  {'type':<26} {'result':<12} {'coverage':<12} note")
        self.stdout.write(f"  {'-' * 26} {'-' * 12} {'-' * 12} {'-' * 40}")

        samples = {}
        for stat_type, purpose in CANDIDATE_TYPES:
            result = service.get_vehicle_stats(vehicle_ids=ids, types=(stat_type,))
            status = result.get("status")

            if status != "success":
                self.stdout.write(
                    f"  {stat_type:<26} {self.style.ERROR(f'{status:<12}')} "
                    f"{'':<12} {str(result.get('error'))[:40]}"
                )
                continue

            records = result.get("data", [])
            reporting = [r for r in records if _has_value(r, stat_type)]
            coverage = f"{len(reporting)}/{len(ids)}"

            if reporting:
                style = self.style.SUCCESS if len(reporting) == len(ids) else self.style.WARNING
                self.stdout.write(
                    f"  {stat_type:<26} {style(f'{_OK:<12}')} {coverage:<12} {purpose}"
                )
                samples[stat_type] = reporting[0]
                # Name the cars that DIDN'T report — that's the gateway-mix answer.
                if len(reporting) < len(ids):
                    silent = [
                        f"#{by_id[str(r.get('id'))].vehicle_number}"
                        for r in records
                        if not _has_value(r, stat_type) and str(r.get("id")) in by_id
                    ]
                    if silent:
                        self.stdout.write(
                            f"  {'':<26} {'':<12} {'':<12} "
                            f"no data from: {', '.join(sorted(silent))}"
                        )
            else:
                self.stdout.write(
                    f"  {stat_type:<26} {self.style.WARNING(f'{_NO:<12}')} "
                    f"{coverage:<12} entitled but no vehicle reported a value"
                )

        self.stdout.write("")
        self._summarize(samples)

        if raw and samples:
            self.stdout.write(self.style.MIGRATE_HEADING("RAW SAMPLES"))
            for stat_type, record in samples.items():
                self.stdout.write(f"\n--- {stat_type} ---")
                self.stdout.write(json.dumps(record, indent=2, default=str)[:1500])
            self.stdout.write("")

    def _summarize(self, samples):
        """Turn the probe into the three schema decisions it exists to answer."""
        self.stdout.write(self.style.MIGRATE_HEADING("WHAT THIS MEANS FOR THE SCHEMA"))

        has_obd = "obdOdometerMeters" in samples
        has_gps_dist = "gpsDistanceMeters" in samples
        has_hours = "obdEngineSeconds" in samples

        if has_obd:
            self.stdout.write(self.style.SUCCESS(
                "  mileage:      obdOdometerMeters available -> primary source confirmed."
            ))
        elif has_gps_dist:
            self.stdout.write(self.style.WARNING(
                "  mileage:      NO obdOdometerMeters. Every mileage figure will be a\n"
                "                GPS-delta ESTIMATE. Say so in the UI before anyone\n"
                "                schedules maintenance off it."
            ))
        else:
            self.stdout.write(self.style.ERROR(
                "  mileage:      neither OBD odometer nor GPS distance available.\n"
                "                Drop mileage + preventive-maintenance-by-mileage entirely."
            ))

        if has_hours:
            self.stdout.write(self.style.SUCCESS(
                "  engine hours: obdEngineSeconds available -> the column can be populated."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "  engine hours: obdEngineSeconds NOT available (expected on light-duty\n"
                "                OBD-II). Keep the column nullable; build nothing on it."
            ))

        readiness = [t for t in ("fuelPercents", "batteryMilliVolts", "engineStates") if t in samples]
        if readiness:
            self.stdout.write(self.style.SUCCESS(
                f"  readiness:    {', '.join(readiness)} available -> chips are worth building."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "  readiness:    no fuel/battery/engine-state -> cut the readiness chips."
            ))

        if "faultCodes" in samples:
            self.stdout.write(self.style.SUCCESS(
                "  faults:       faultCodes available via stats -> no webhook needed for them."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "  faults:       no faultCodes on the stats endpoint. Check whether\n"
                "                /fleet/maintenance/list returned 200 above instead."
            ))
        self.stdout.write("")


def _has_value(record, stat_type):
    """
    True when this record carries a usable value for the type.

    Goes through _stat_block, which knows that Samsara does NOT always echo the
    requested type name back: `fuelPercents` returns as `fuelPercent` and
    `engineStates` as `engineState`. Checking the requested name directly made
    this probe report "0/11 — entitled but nothing reported" for two types the
    Samsara dashboard was displaying fine. A probe that lies is worse than no
    probe, so the lookup lives in one shared place.
    """
    from dispatching.samsara_service import _stat_block

    block = _stat_block(record, stat_type)
    if block is not None:
        if "value" in block:
            return block.get("value") is not None
        return bool(block)  # gps is a dict of its own fields

    raw = (record or {}).get(stat_type)
    if isinstance(raw, list):
        return len(raw) > 0
    return raw is not None
