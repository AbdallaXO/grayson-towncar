"""
Fleet Management pages: a searchable vehicle list and a per-vehicle detail page,
plus the JSON endpoints that let a dispatcher run the whole fleet job from those
two pages instead of the Django admin.

Editing lives HERE, not in admin, by explicit request. The split is:
  * A human owns compliance dates, service intervals and service records.
  * The poller owns every samsara_* column and VehicleDayReading.
Nothing below writes a poller-owned field — a hand edit there would be silently
overwritten within three minutes, and a typo'd odometer would corrupt the next
day's mileage delta.

DB-ONLY. These views never call Samsara. Two hard runtime ceilings make that
non-negotiable: reservations/middleware.py sets a 30-second Postgres
statement_timeout on web requests, and railway.json runs gunicorn with
--timeout 60. A synchronous external call in a render path already caused a
worker-timeout incident once (docs/Samsara_feature_handoff.md). All collection
happens in the background poller; all aggregation happens in the nightly.

Rendering rules enforced here and in the templates:
  * NULL mileage renders as an em-dash, never 0. Zero means the car provably did
    not move; a dash means we do not know. Conflating them makes a dead gateway
    look like a parked car.
  * Every derived number carries its provenance (obd = exact, gps = estimate).
  * Every total states its coverage.
"""
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from dispatching import fleet_health
from dispatching.fleet_sync import FEED_NIGHTLY, FEED_VEHICLE_STATS
from dispatching.mileage import days_to_cover, meters_to_miles, usage_rate
from dispatching.samsara_service import EXTENDED_STAT_TYPES
from drivers.models import (
    DriverVehicleAssignment, FleetSyncState, FleetVehicle, VehicleDayReading,
    VehicleFault, VehicleServiceRecord, VehicleServiceSchedule,
)

# How much recent history the detail page shows. Small on purpose — this is an
# operations page, not an analytics tool.
DETAIL_DAY_WINDOW = 30


def _natural_key(vehicle_number):
    """
    Sort '001' < '10' < '13' the way a human reads a unit board.

    The fleet numbers this company uses are a mix of zero-padded ('001') and
    plain ('10'), so a plain string sort puts #10 before #002.
    """
    number = (vehicle_number or "").strip()
    digits = "".join(ch for ch in number if ch.isdigit())
    return (0, int(digits), number) if digits else (1, 0, number)


@login_required
@staff_member_required
def fleet_list(request):
    """Every vehicle in one searchable, filterable list."""
    search = (request.GET.get("q") or "").strip()
    status = request.GET.get("status", "active")   # active | inactive | all
    coverage = request.GET.get("coverage", "all")  # all | mapped | unmapped
    sort = request.GET.get("sort", "number")       # number | odometer | fuel | attention

    now = timezone.now()
    today = timezone.localdate(now)

    vehicles_qs = FleetVehicle.objects.select_related("vehicle_type")

    if status == "active":
        vehicles_qs = vehicles_qs.filter(is_active=True)
    elif status == "inactive":
        vehicles_qs = vehicles_qs.filter(is_active=False)

    if coverage == "mapped":
        vehicles_qs = vehicles_qs.exclude(samsara_vehicle_id="")
    elif coverage == "unmapped":
        # The onboarding backlog, finally visible in the product instead of only
        # in `samsara_sync_vehicles --list-mappings` on someone's terminal.
        vehicles_qs = vehicles_qs.filter(samsara_vehicle_id="")

    if search:
        vehicles_qs = vehicles_qs.filter(
            Q(vehicle_number__icontains=search)
            | Q(make__icontains=search)
            | Q(model__icontains=search)
            | Q(vin__icontains=search)
            | Q(license_plate__icontains=search)
            | Q(samsara_name__icontains=search)
            | Q(transponder_number__icontains=search)
        )

    vehicles_qs = vehicles_qs.annotate(
        open_faults=Count("faults", filter=Q(faults__resolved_at__isnull=True),
                          distinct=True),
    )

    vehicles = list(vehicles_qs)
    vehicle_ids = [v.id for v in vehicles]

    # One query each for the things every row needs — no per-row lookups.
    shop_records = {}
    for record in VehicleServiceRecord.objects.filter(
        vehicle_id__in=vehicle_ids, out_of_service_from__isnull=False
    ):
        shop_records.setdefault(record.vehicle_id, []).append(record)

    schedules = {}
    for schedule in VehicleServiceSchedule.objects.filter(
        vehicle_id__in=vehicle_ids, is_active=True
    ):
        schedules.setdefault(schedule.vehicle_id, []).append(schedule)

    window_start = today - timedelta(days=DETAIL_DAY_WINDOW)
    recent_miles = {
        row["vehicle_id"]: row
        for row in VehicleDayReading.objects.filter(
            vehicle_id__in=vehicle_ids, date__gte=window_start, date__lte=today
        )
        .values("vehicle_id")
        .annotate(
            miles=Sum("miles_driven"),
            known_days=Count("id", filter=Q(miles_driven__isnull=False)),
            total_days=Count("id"),
        )
    }
    # Sum/count already exclude NULL days on both sides, which is exactly the
    # unknown-vs-parked rule usage_rate() enforces — so the per-day figure here
    # matches the detail page's rather than being a second, subtly different
    # average. Kept as one aggregate query: this page is 9 queries flat.

    rows = []
    for vehicle in vehicles:
        in_shop = fleet_health.is_in_shop(shop_records.get(vehicle.id, []), today)
        chips = fleet_health.vehicle_readiness(
            vehicle, now, open_fault_count=vehicle.open_faults, in_shop=in_shop
        )
        chips += fleet_health.compliance_findings(vehicle, today)

        odometer = vehicle.odometer_miles
        for schedule in schedules.get(vehicle.id, []):
            chips += fleet_health.service_findings(schedule, odometer, today)

        miles = recent_miles.get(vehicle.id) or {}
        _known_days = miles.get("known_days") or 0
        _miles = miles.get("miles")
        _per_day = (
            (Decimal(_miles) / _known_days).quantize(Decimal("0.1"))
            if _miles is not None and _known_days else None
        )
        rows.append({
            "vehicle": vehicle,
            "chips": chips,
            # Per WEEK on the list: comparing "which car works hardest" reads
            # better at week scale than a daily figure that swings with one
            # airport run. None stays None — an unknown rate is not a low one.
            "per_week": (_per_day * 7).quantize(Decimal("0.1")) if _per_day is not None else None,
            "per_day": _per_day,
            # Resolved here, not in the template, so "expired counts as missing"
            # is decided in exactly one place (FleetVehicle.permits).
            "permits": vehicle.permits(day=today),
            "oos_label": vehicle.out_of_service_label(today),
            # None when the car has never reported a level — the template shows
            # an em-dash, never an empty gauge that reads as "empty tank".
            "fuel": fleet_health.fuel_reading(vehicle, now),
            "attention": sum(1 for c in chips if c["level"] == fleet_health.CRITICAL),
            "warnings": sum(1 for c in chips if c["level"] == fleet_health.WARN),
            "odometer": odometer,
            "odometer_estimated": vehicle.odometer_is_estimate,
            # None (not 0) when nothing is known — the template renders an em-dash.
            "recent_miles": miles.get("miles"),
            "coverage": fleet_health.summarise_coverage(
                miles.get("known_days", 0), miles.get("total_days", 0)
            ),
        })

    if sort == "odometer":
        # Unknown odometers sort last rather than as 0.
        rows.sort(key=lambda r: (r["odometer"] is None, -(r["odometer"] or 0)))
    elif sort == "fuel":
        # Emptiest first — this sort exists to answer "who am I sending out for
        # gas tonight". An unknown level sorts LAST, not as an empty tank.
        rows.sort(key=lambda r: (r["fuel"] is None,
                                 r["fuel"]["percent"] if r["fuel"] else 0,
                                 _natural_key(r["vehicle"].vehicle_number)))
    elif sort == "attention":
        rows.sort(key=lambda r: (-r["attention"], -r["warnings"],
                                 _natural_key(r["vehicle"].vehicle_number)))
    else:
        rows.sort(key=lambda r: _natural_key(r["vehicle"].vehicle_number))

    stats_state = FleetSyncState.objects.filter(feed=FEED_VEHICLE_STATS).first()
    nightly_state = FleetSyncState.objects.filter(feed=FEED_NIGHTLY).first()

    context = {
        "rows": rows,
        "search": search,
        "status_filter": status,
        "coverage_filter": coverage,
        "sort": sort,
        "total_vehicles": len(rows),
        "total_active": sum(1 for r in rows if r["vehicle"].is_active),
        "total_mapped": sum(1 for r in rows if r["vehicle"].samsara_vehicle_id),
        "total_attention": sum(1 for r in rows if r["attention"]),
        # The highest-value pixel on the page: is data arriving at all?
        "feed": fleet_health.feed_health(stats_state, now),
        "nightly": nightly_state,
        "window_days": DETAIL_DAY_WINDOW,
        # Bulk editor. Permit keys come from the model's own tuple so a fourth
        # permit is a migration and nothing else — the form, the payload and the
        # confirmation sentence all pick it up.
        "permit_types": FleetVehicle.PERMITS,
        "transponder_types": FleetVehicle.TRANSPONDER_TYPE_CHOICES,
    }
    return render(request, "dispatching/fleet_list.html", context)


@login_required
@staff_member_required
def fleet_detail(request, pk):
    """Everything known about one physical car."""
    now = timezone.now()
    today = timezone.localdate(now)

    vehicle = get_object_or_404(
        FleetVehicle.objects.select_related("vehicle_type"), pk=pk
    )

    service_records = list(
        VehicleServiceRecord.objects.filter(vehicle=vehicle)
        .select_related("created_by")[:25]
    )
    in_shop = fleet_health.is_in_shop(service_records, today)

    open_faults = list(
        VehicleFault.objects.filter(vehicle=vehicle, resolved_at__isnull=True)
    )
    recent_faults = list(
        VehicleFault.objects.filter(vehicle=vehicle, resolved_at__isnull=False)[:10]
    )

    chips = fleet_health.vehicle_readiness(
        vehicle, now, open_fault_count=len(open_faults), in_shop=in_shop
    )
    chips += fleet_health.compliance_findings(vehicle, today)

    odometer = vehicle.odometer_miles

    window_start = today - timedelta(days=DETAIL_DAY_WINDOW)
    days = list(
        VehicleDayReading.objects.filter(
            vehicle=vehicle, date__gte=window_start, date__lte=today
        ).order_by("-date")
    )
    # None, not 0, when no day in the window has a known figure — the template
    # renders it as an em-dash so "no data" never reads as "did not move".
    known = [d for d in days if d.miles_driven is not None]
    total_miles = sum(d.miles_driven for d in known) if known else None

    # How hard this car actually works. The arithmetic (and the unknown-vs-parked
    # rule it turns on) lives in mileage.py — see the module docstring for why
    # nothing else may compute a mileage figure.
    rate = usage_rate([d.miles_driven for d in days], total_days=DETAIL_DAY_WINDOW)

    # The odometer at both ends of each day. Already stored on every row and
    # never surfaced until now: "335.5 mi" is a number you have to trust, while
    # "104,210 → 104,545" is one you can check against the dash.
    for day in days:
        day.start_miles = meters_to_miles(day.start_odometer_meters, places=0)
        day.end_miles = meters_to_miles(day.end_odometer_meters, places=0)

    schedules = []
    for schedule in VehicleServiceSchedule.objects.filter(
        vehicle=vehicle, is_active=True
    ):
        findings = fleet_health.service_findings(schedule, odometer, today)
        chips += findings
        # Turn "due in 2,400 mi" into a date someone can book a shop slot for.
        # Advisory and explicitly rate-based: it says "at this rate", and it
        # declines entirely when the rate is unknown or the car isn't moving,
        # rather than emitting a date nobody should plan around.
        due_miles = schedule.due_at_odometer_miles
        miles_remaining = (
            due_miles - Decimal(odometer)
            if due_miles is not None and odometer is not None else None
        )
        days_out = days_to_cover(miles_remaining, rate.per_day)
        schedules.append({
            "schedule": schedule,
            "findings": findings,
            "miles_remaining": miles_remaining,
            "projected_days": days_out,
            "projected_date": today + timedelta(days=days_out) if days_out else None,
        })

    # Who has been in this car lately — the only job<->physical-car link that
    # exists, since Leg.vehicle points at the TYPE (rates.Vehicle), never here.
    assignments = list(
        DriverVehicleAssignment.objects
        .filter(vehicle=vehicle, date__gte=window_start, date__lte=today)
        .select_related("driver")
        .order_by("-date")[:20]
    )

    context = {
        "vehicle": vehicle,
        "chips": sorted(chips, key=lambda c: {"critical": 0, "warn": 1, "info": 2}[c["level"]]),
        "odometer": odometer,
        "odometer_estimated": vehicle.odometer_is_estimate,
        "in_shop": in_shop,
        "schedules": schedules,
        "service_records": service_records,
        "open_faults": open_faults,
        "recent_faults": recent_faults,
        "days": days,
        "total_miles": total_miles,
        "rate": rate,
        "coverage": fleet_health.summarise_coverage(len(known), len(days)),
        "assignments": assignments,
        "window_days": DETAIL_DAY_WINDOW,
        "feed": fleet_health.feed_health(
            FleetSyncState.objects.filter(feed=FEED_VEHICLE_STATS).first(), now
        ),
        "service_types": VehicleServiceRecord.SERVICE_TYPE_CHOICES,
        "schedule_types": VehicleServiceSchedule.SERVICE_TYPE_CHOICES,
        "transponder_types": FleetVehicle.TRANSPONDER_TYPE_CHOICES,
        # Permits + out-of-service, resolved server-side against TODAY so the page
        # and every scheduling surface answer the same question the same way.
        "vehicle_permits": vehicle.permits(),
        "oos_label": vehicle.out_of_service_label(),
        # Derived from what we actually ASK Samsara for, so these labels stay
        # true on their own — drop a type from EXTENDED_STAT_TYPES and the page
        # starts saying "not reported" instead of showing an em-dash that would
        # read as "pending" forever. fuelPercents and engineStates first probed
        # as absent because the response key is SINGULAR (fuelPercent /
        # engineState); once aliased they came back 11/11 and are collected.
        "fuel_collected": "fuelPercents" in EXTENDED_STAT_TYPES,
        "engine_state_collected": "engineStates" in EXTENDED_STAT_TYPES,
        "engine_hours_collected": "obdEngineSeconds" in EXTENDED_STAT_TYPES,
        "engine_hours": (
            round(vehicle.samsara_engine_seconds / 3600)
            if vehicle.samsara_engine_seconds else None
        ),
    }
    return render(request, "dispatching/fleet_detail.html", context)


# ════════════════════════════════════════════════════════════════════════════
# Edit endpoints — everything a dispatcher needs, without the Django admin
#
# House shape (dispatching/views.py admin_travel_agent_* endpoints): POST-only,
# staff-only, JSON body in, {"success": bool, ...} out, never a 500 for bad user
# input. Errors carry a sentence a dispatcher can act on.
# ════════════════════════════════════════════════════════════════════════════

def _body(request):
    try:
        return json.loads(request.body or "{}"), None
    except json.JSONDecodeError:
        return None, JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)


def _opt_date(raw, label):
    """'' / None -> None (clearing a date is legitimate). Bad text -> error."""
    if raw in (None, ""):
        return None, None
    parsed = parse_date(str(raw))
    if parsed is None:
        return None, f"{label} must be a date (YYYY-MM-DD)."
    return parsed, None


def _opt_decimal(raw, label, *, minimum=None):
    if raw in (None, ""):
        return None, None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None, f"{label} must be a number."
    if minimum is not None and value < minimum:
        return None, f"{label} can't be less than {minimum}."
    return value, None


def _opt_int(raw, label, *, minimum=None):
    if raw in (None, ""):
        return None, None
    try:
        value = int(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None, f"{label} must be a whole number."
    if minimum is not None and value < minimum:
        return None, f"{label} can't be less than {minimum}."
    return value, None


def _collect_vehicle_fields(data):
    """
    Coerce a details payload into ``{model_field: value}``, or return a sentence
    explaining what's wrong with it.

    Shared by the single-vehicle form and the bulk editor, deliberately: every
    rule below (an expiry can't outlive its tick, a date must parse, a reason
    truncates at 200) has to hold identically whether it's typed on one car or
    stamped onto twelve. A second copy of these rules would drift.

    Key absent  = leave the field alone.
    Key present but empty = clear it. Clearing a date is legitimate.

    The out-of-service WINDOW check is not here — it compares against what's
    already stored on each vehicle, so it runs per-vehicle in _oos_window_error.
    """
    fields = {}

    for key, label in (
        ("in_service_since", "In-service date"),
        ("registration_expires_on", "Registration expiry"),
        ("insurance_expires_on", "Insurance expiry"),
        ("next_inspection_on", "Next inspection"),
    ):
        if key not in data:
            continue
        value, message = _opt_date(data.get(key), label)
        if message:
            return None, message
        fields[key] = value

    if "notes" in data:
        fields["notes"] = (data.get("notes") or "").strip()

    if "transponder_number" in data:
        fields["transponder_number"] = (data.get("transponder_number") or "").strip()[:32]
    if "transponder_type" in data:
        transponder_type = (data.get("transponder_type") or "").strip()
        valid = {c[0] for c in FleetVehicle.TRANSPONDER_TYPE_CHOICES}
        if transponder_type and transponder_type not in valid:
            return None, "Unknown transponder type."
        fields["transponder_type"] = transponder_type

    # ── Out of service ───────────────────────────────────────────────────
    # The one field here that removes a unit from the scheduling pool, so it's
    # the one field that gets validated hard.
    for key, label in (("out_of_service_from", "Out-of-service start"),
                       ("out_of_service_until", "Out-of-service end")):
        if key not in data:
            continue
        value, message = _opt_date(data.get(key), label)
        if message:
            return None, message
        fields[key] = value
    if "out_of_service_reason" in data:
        fields["out_of_service_reason"] = (
            data.get("out_of_service_reason") or "").strip()[:200]

    # ── Permits ──────────────────────────────────────────────────────────
    for key, label, _category in FleetVehicle.PERMITS:
        held_field = f"permit_{key}"
        expiry_field = f"permit_{key}_expires_on"
        if held_field in data:
            fields[held_field] = bool(data.get(held_field))
        if expiry_field in data:
            value, message = _opt_date(data.get(expiry_field), f"{label} permit expiry")
            if message:
                return None, message
            fields[expiry_field] = value
        # An expiry with no permit is a contradiction. Clearing the tick clears
        # the date with it, so a permit that comes back doesn't inherit a stale one.
        if fields.get(held_field) is False:
            fields[expiry_field] = None

    return fields, None


def _oos_window_error(vehicle, fields):
    """
    The out-of-service window this edit would leave on ``vehicle``, checked
    against what's already stored. Returns a sentence, or None when it's sound.

    A backwards window would silently never match any date — the unit would look
    blocked on the form and stay bookable everywhere else.
    """
    start = fields.get("out_of_service_from", vehicle.out_of_service_from)
    end = fields.get("out_of_service_until", vehicle.out_of_service_until)
    if start and end and end < start:
        return "the out-of-service end date is before the start date."
    # An end date with no start is not a window — it gates nothing and would sit
    # on the record looking meaningful. Refuse it rather than store a no-op.
    if end and not start:
        return "it needs an out-of-service start date, or a cleared end date."
    return None


@login_required
@staff_member_required
@require_POST
def fleet_update_details(request, pk):
    """
    JSON: compliance dates + notes on one vehicle.

    Only fields a HUMAN owns. VIN, plate and every samsara_* column are absent
    on purpose — those come from Samsara and an edit here would be overwritten
    by the next poll or the nightly master refresh.
    """
    vehicle = get_object_or_404(FleetVehicle, pk=pk)
    data, error = _body(request)
    if error:
        return error

    fields, message = _collect_vehicle_fields(data)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    if not fields:
        return JsonResponse({"success": False, "error": "Nothing to update."}, status=400)

    message = _oos_window_error(vehicle, fields)
    if message:
        # Single-vehicle: the dispatcher is looking at the car, so don't name it.
        return JsonResponse(
            {"success": False, "error": message[0].upper() + message[1:]}, status=400)

    for key, value in fields.items():
        setattr(vehicle, key, value)
    vehicle.save(update_fields=list(fields))
    return JsonResponse({"success": True})


# What a BULK edit is allowed to touch. Deliberately narrower than the
# single-vehicle form, and the difference is the point:
#   * transponder_number is a per-car identity. Stamping one number onto twelve
#     cars doesn't save keystrokes, it produces twelve wrong toll attributions.
#   * notes is free text somebody already wrote. A bulk overwrite destroys it
#     with no undo and no way to tell which cars had something worth keeping.
# Everything left is a fact that genuinely IS the same across a batch: a decal
# run bought together, a policy renewed on one date, a shop closure.
BULK_EDITABLE = frozenset(
    ["in_service_since", "registration_expires_on", "insurance_expires_on",
     "next_inspection_on", "transponder_type",
     "out_of_service_from", "out_of_service_until", "out_of_service_reason"]
    + [f"permit_{key}" for key, _l, _c in FleetVehicle.PERMITS]
    + [f"permit_{key}_expires_on" for key, _l, _c in FleetVehicle.PERMITS]
)

# A selection larger than the whole fleet is a bug in the caller, not a request.
BULK_LIMIT = 500


@login_required
@staff_member_required
@require_POST
def fleet_bulk_update(request):
    """
    JSON: apply ONE set of values to MANY vehicles.

    Body: {"vehicle_ids": [1, 2, 3], "fields": {"permit_mco": true, ...}}

    Built for data entry, which is why it behaves the way it does:

      * ONLY the keys present in ``fields`` are written. A blank input on the
        form is not sent at all, so bulk-setting an insurance date can never
        quietly wipe three permits the operator wasn't looking at.
      * ALL OR NOTHING. Every selected unit is validated before anything is
        written, and the write is one atomic statement. Half a batch applied,
        with an error message naming a car in the middle, is the worst possible
        outcome for someone typing from a stack of paperwork — they can't tell
        what landed.
      * The error names the UNIT, not the row number. "#12: the out-of-service
        end date is before the start date" is actionable; "row 7 invalid" isn't.
    """
    data, error = _body(request)
    if error:
        return error

    raw_ids = data.get("vehicle_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return JsonResponse(
            {"success": False, "error": "Pick at least one vehicle."}, status=400)
    if len(raw_ids) > BULK_LIMIT:
        return JsonResponse(
            {"success": False,
             "error": f"That's more than {BULK_LIMIT} vehicles at once."}, status=400)
    try:
        ids = {int(value) for value in raw_ids}
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "error": "That vehicle selection isn't valid."},
            status=400)

    payload = data.get("fields")
    if not isinstance(payload, dict) or not payload:
        return JsonResponse(
            {"success": False,
             "error": "Nothing to change — tick a field before applying."}, status=400)

    refused = sorted(set(payload) - BULK_EDITABLE)
    if refused:
        # Named explicitly rather than silently dropped: a caller that thought it
        # was setting notes on 12 cars should hear that it wasn't.
        return JsonResponse({
            "success": False,
            "error": "These can only be edited one car at a time: "
                     + ", ".join(refused) + ".",
        }, status=400)

    fields, message = _collect_vehicle_fields(payload)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    if not fields:
        return JsonResponse(
            {"success": False,
             "error": "Nothing to change — tick a field before applying."}, status=400)

    vehicles = list(FleetVehicle.objects.filter(id__in=ids))
    if not vehicles:
        return JsonResponse(
            {"success": False, "error": "Those vehicles no longer exist. Reload the page."},
            status=400)

    for vehicle in vehicles:
        message = _oos_window_error(vehicle, fields)
        if message:
            return JsonResponse({
                "success": False,
                "error": f"#{vehicle.vehicle_number}: {message} "
                         f"Nothing was changed.",
            }, status=400)

    with transaction.atomic():
        updated = FleetVehicle.objects.filter(
            id__in=[v.id for v in vehicles]).update(**fields)

    return JsonResponse({
        "success": True,
        "updated": updated,
        # A selection that outlived the row it pointed at — someone deleted a
        # vehicle in another tab. Reported, never silently absorbed.
        "missing": len(ids) - len(vehicles),
    })


@login_required
@staff_member_required
@require_POST
def fleet_save_schedule(request, pk):
    """
    JSON: create or update one maintenance interval on a vehicle.

    Upserts on (vehicle, service_type) — the model's unique key — so re-saving
    the same type edits the existing row instead of raising IntegrityError.
    """
    vehicle = get_object_or_404(FleetVehicle, pk=pk)
    data, error = _body(request)
    if error:
        return error

    service_type = (data.get("service_type") or "").strip()
    valid = {c[0] for c in VehicleServiceSchedule.SERVICE_TYPE_CHOICES}
    if service_type not in valid:
        return JsonResponse(
            {"success": False, "error": "Pick a service type."}, status=400)

    interval_miles, message = _opt_int(data.get("interval_miles"), "Mileage interval", minimum=1)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    interval_days, message = _opt_int(data.get("interval_days"), "Day interval", minimum=1)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)

    if interval_miles is None and interval_days is None:
        # A schedule with neither can never come due — it would sit on the page
        # looking active while silently doing nothing.
        return JsonResponse({
            "success": False,
            "error": "Set a mileage interval, a day interval, or both.",
        }, status=400)

    last_done_on, message = _opt_date(data.get("last_done_on"), "Last done date")
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    last_odo, message = _opt_decimal(
        data.get("last_done_odometer_miles"), "Last done odometer", minimum=0)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)

    schedule, created = VehicleServiceSchedule.objects.update_or_create(
        vehicle=vehicle,
        service_type=service_type,
        defaults={
            "interval_miles": interval_miles,
            "interval_days": interval_days,
            "last_done_on": last_done_on,
            "last_done_odometer_miles": last_odo,
            "is_active": bool(data.get("is_active", True)),
            "notes": (data.get("notes") or "").strip(),
        },
    )
    return JsonResponse({"success": True, "created": created, "id": schedule.id})


@login_required
@staff_member_required
@require_POST
def fleet_delete_schedule(request, pk):
    """JSON: remove a maintenance interval."""
    schedule = get_object_or_404(VehicleServiceSchedule, pk=pk)
    schedule.delete()
    return JsonResponse({"success": True})


@login_required
@staff_member_required
@require_POST
def fleet_add_service(request, pk):
    """
    JSON: log a service that happened.

    Side effect worth knowing about: if an active schedule exists for the same
    service type, its last-done date and odometer advance to this record. That
    is the point — logging an oil change should reset the oil interval without
    anyone re-typing it in a second place. Only advances forward, so
    back-filling an older receipt can't un-do a newer service.
    """
    vehicle = get_object_or_404(FleetVehicle, pk=pk)
    data, error = _body(request)
    if error:
        return error

    service_type = (data.get("service_type") or "").strip()
    valid = {c[0] for c in VehicleServiceRecord.SERVICE_TYPE_CHOICES}
    if service_type not in valid:
        return JsonResponse({"success": False, "error": "Pick a service type."}, status=400)

    performed_on, message = _opt_date(data.get("performed_on"), "Date performed")
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    if performed_on is None:
        return JsonResponse(
            {"success": False, "error": "Date performed is required."}, status=400)
    if performed_on > timezone.localdate():
        return JsonResponse(
            {"success": False, "error": "Date performed can't be in the future."},
            status=400)

    odometer, message = _opt_decimal(data.get("odometer_miles"), "Odometer", minimum=0)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    cost, message = _opt_decimal(data.get("cost"), "Cost", minimum=0)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)

    oos_from, message = _opt_date(data.get("out_of_service_from"), "Out-of-service start")
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    oos_to, message = _opt_date(data.get("out_of_service_to"), "Out-of-service end")
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    if oos_from and oos_to and oos_to < oos_from:
        return JsonResponse(
            {"success": False, "error": "Out-of-service end is before its start."},
            status=400)
    if oos_to and not oos_from:
        return JsonResponse({
            "success": False,
            "error": "Give an out-of-service start date as well as an end date.",
        }, status=400)

    record = VehicleServiceRecord.objects.create(
        vehicle=vehicle,
        service_type=service_type,
        performed_on=performed_on,
        odometer_miles=odometer,
        vendor=(data.get("vendor") or "").strip()[:120],
        cost=cost,
        description=(data.get("description") or "").strip(),
        out_of_service_from=oos_from,
        out_of_service_to=oos_to,
        fault_reference=(data.get("fault_reference") or "").strip()[:120],
        created_by=request.user,
    )

    advanced = _advance_schedule(vehicle, service_type, performed_on, odometer)
    return JsonResponse({
        "success": True,
        "id": record.id,
        "schedule_advanced": advanced,
    })


def _advance_schedule(vehicle, service_type, performed_on, odometer):
    """
    Move the matching interval's baseline forward. Returns True if it moved.

    Guarded against going backwards: logging a receipt from three months ago
    must not reset an interval that a more recent service already advanced.
    """
    schedule = VehicleServiceSchedule.objects.filter(
        vehicle=vehicle, service_type=service_type, is_active=True
    ).first()
    if schedule is None:
        return False

    changed = []
    if schedule.last_done_on is None or performed_on > schedule.last_done_on:
        schedule.last_done_on = performed_on
        changed.append("last_done_on")
    if odometer is not None and (
        schedule.last_done_odometer_miles is None
        or odometer > schedule.last_done_odometer_miles
    ):
        schedule.last_done_odometer_miles = odometer
        changed.append("last_done_odometer_miles")

    if not changed:
        return False
    schedule.save(update_fields=changed)
    return True


@login_required
@staff_member_required
@require_POST
def fleet_delete_service(request, pk):
    """
    JSON: remove a service record.

    Deliberately does NOT rewind the schedule baseline — recomputing which of
    the remaining records should own it is guesswork, and a silently-rewound
    interval is worse than a stale one. Re-save the schedule to correct it.
    """
    record = get_object_or_404(VehicleServiceRecord, pk=pk)
    record.delete()
    return JsonResponse({"success": True})
