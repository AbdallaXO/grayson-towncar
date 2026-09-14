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
from django.views.decorators.http import require_GET, require_POST

from dispatching import fleet_capacity, fleet_health, fleet_notify
# Aliased: the views below are named fleet_desk / fleet_report after their URLs,
# and a bare module import would be shadowed by the function definitions.
from dispatching import fleet_desk as fleet_desk_loader
from dispatching import fleet_report as fleet_reporting
from dispatching.fleet_sync import FEED_NIGHTLY, FEED_VEHICLE_STATS
from dispatching.mileage import days_to_cover, meters_to_miles, usage_rate
from dispatching.samsara_service import EXTENDED_STAT_TYPES
from drivers.models import (
    DriverVehicleAssignment, FleetSyncState, FleetVehicle, VehicleDayReading,
    VehicleDowntime, VehicleFault, VehicleIssue, VehicleServiceRecord,
    VehicleServiceSchedule,
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


@login_required(login_url="login")
@staff_member_required
def fleet_list(request):
    """Every vehicle in one searchable, filterable list."""
    search = (request.GET.get("q") or "").strip()
    status = request.GET.get("status", "active")   # active | inactive | all
    coverage = request.GET.get("coverage", "all")  # all | mapped | unmapped
    sort = request.GET.get("sort", "number")       # number | odometer | fuel | attention

    now = timezone.now()
    today = timezone.localdate(now)

    vehicles_qs = FleetVehicle.objects.select_related("vehicle_type").with_open_downtimes()

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
        "fleet_page": "vehicles",
    }
    return render(request, "dispatching/fleet_list.html", context)


@login_required(login_url="login")
@staff_member_required
def fleet_detail(request, pk):
    """Everything known about one physical car."""
    now = timezone.now()
    today = timezone.localdate(now)

    vehicle = get_object_or_404(
        FleetVehicle.objects.select_related("vehicle_type").with_open_downtimes(), pk=pk
    )

    service_records = list(
        VehicleServiceRecord.objects.filter(vehicle=vehicle)
        .select_related("created_by")[:25]
    )
    in_shop = fleet_health.is_in_shop(service_records, today)

    # ── Downtime ledger + reported issues ────────────────────────────────
    downtimes_open = vehicle.open_downtimes()
    downtimes_closed = list(
        VehicleDowntime.objects.filter(vehicle=vehicle, ended_on__isnull=False)
        .select_related("closed_by")[:10]
    )
    for d in downtimes_open + downtimes_closed:
        d.days = d.days_down(today)
        d.state = ("overdue" if d.is_overdue(today) else "planned" if d.is_planned(today)
                   else "live" if d.is_live(today) else "closed")
    issues_open = list(
        VehicleIssue.objects.filter(vehicle=vehicle, resolved_at__isnull=True)
        .select_related("reported_by")
    )
    issues_resolved = list(
        VehicleIssue.objects.filter(vehicle=vehicle, resolved_at__isnull=False)
        .select_related("reported_by", "resolved_by")[:10]
    )
    downtime_days_90 = sum(
        d.days_down(today) or 0
        for d in VehicleDowntime.objects.filter(
            vehicle=vehicle, starts_on__gte=today - timedelta(days=90), starts_on__lte=today)
    )

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
        "oos_notice": vehicle.downtime_notice(),
        "downtimes_open": downtimes_open,
        "downtimes_closed": downtimes_closed,
        "issues_open": issues_open,
        "issues_resolved": issues_resolved,
        "downtime_days_90": downtime_days_90,
        "downtime_categories": VehicleDowntime.CATEGORY_CHOICES,
        "issue_severities": VehicleIssue.SEVERITY_CHOICES,
        "issue_sources": VehicleIssue.SOURCE_CHOICES,
        "standard_intervals": STANDARD_INTERVALS,
        "today_date": today,
        "fleet_page": "vehicles",
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

    # Out of service is NOT here any more: it lives in the downtime ledger
    # (fleet_save_downtime / fleet_close_downtime), one row per shop visit.
    for key in ("out_of_service_from", "out_of_service_until", "out_of_service_reason"):
        if key in data:
            return None, ("Out of service is recorded as a downtime now — use "
                          "'Take out of service' on the vehicle page.")

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


@login_required(login_url="login")
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
# run bought together, a policy renewed on one date.
#   * out of service is not here either: a downtime carries a reason, a shop
#     and a return date PER CAR, and is recorded on the vehicle's own page.
BULK_EDITABLE = frozenset(
    ["in_service_since", "registration_expires_on", "insurance_expires_on",
     "next_inspection_on", "transponder_type"]
    + [f"permit_{key}" for key, _l, _c in FleetVehicle.PERMITS]
    + [f"permit_{key}_expires_on" for key, _l, _c in FleetVehicle.PERMITS]
)

# A selection larger than the whole fleet is a bug in the caller, not a request.
BULK_LIMIT = 500


@login_required(login_url="login")
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


@login_required(login_url="login")
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


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_delete_schedule(request, pk):
    """JSON: remove a maintenance interval."""
    schedule = get_object_or_404(VehicleServiceSchedule, pk=pk)
    schedule.delete()
    return JsonResponse({"success": True})


@login_required(login_url="login")
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


@login_required(login_url="login")
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


# ════════════════════════════════════════════════════════════════════════════
# The Fleet desk — the fleet manager's home
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url="login")
@staff_member_required
def fleet_desk(request):
    """What needs attention, what's ready, what's down and why, what's coming.

    Everything is loaded by ``fleet_desk.load_desk`` and judged by pure code
    (``fleet_attention``, ``fleet_capacity``), so the page, the morning text
    and the tests read one picture. DB-only, like every fleet page.
    """
    desk = fleet_desk_loader.load_desk()
    context = {
        **desk,
        "fleet_page": "desk",
        "downtime_categories": VehicleDowntime.CATEGORY_CHOICES,
        "issue_severities": VehicleIssue.SEVERITY_CHOICES,
    }
    return render(request, "dispatching/fleet_desk.html", context)


@login_required(login_url="login")
@staff_member_required
def fleet_outlook(request):
    """Four weeks of demand against the fleet, day by day — and, when a unit
    and a window are chosen, whether taking that unit down then leaves
    dispatch short. The page fleet plans shop time from."""
    today = timezone.localdate()
    start = parse_date(request.GET.get("start") or "") or today
    if start < today - timedelta(days=7):
        start = today
    try:
        days = max(7, min(int(request.GET.get("days") or fleet_capacity.DEFAULT_OUTLOOK_DAYS), 56))
    except ValueError:
        days = fleet_capacity.DEFAULT_OUTLOOK_DAYS

    units = fleet_capacity.fleet_units()
    rows = fleet_capacity.outlook(start, days, units, today=today)

    # Optional: check a unit over a window, and offer the best windows for it.
    unit = None
    check = None
    suggestions = []
    try:
        unit_id = int(request.GET.get("unit") or 0)
    except ValueError:
        unit_id = 0
    if unit_id:
        unit = next((u for u in units if u.id == unit_id), None)
    check_from = parse_date(request.GET.get("from") or "")
    check_back = parse_date(request.GET.get("back") or "")
    try:
        length = max(1, min(int(request.GET.get("length") or 1), 14))
    except ValueError:
        length = 1
    if unit is not None:
        if check_from:
            check = fleet_capacity.check_window(unit, check_from, check_back, units, today=today)
        suggestions = fleet_capacity.suggest_windows(unit, length, units, today=today,
                                                     horizon_days=days)

    context = {
        "fleet_page": "outlook",
        "today": today,
        "start": start,
        "days": days,
        "rows": rows,
        "units": units,
        "unit": unit,
        "check": check,
        "check_from": check_from,
        "check_back": check_back,
        "length": length,
        "suggestions": suggestions,
        "type_labels": [(t, fleet_capacity.type_label(t)) for t in fleet_capacity.VEHICLE_TIER_ORDER],
        "prev_start": start - timedelta(days=days),
        "next_start": start + timedelta(days=days),
        "typical_weeks": fleet_capacity.TYPICAL_USE_WEEKS,
    }
    return render(request, "dispatching/fleet_outlook.html", context)


@login_required(login_url="login")
@staff_member_required
@require_GET
def fleet_check_window(request, pk):
    """JSON: would this unit being down from ``from`` until ``back`` leave
    dispatch short? Read by the downtime form as the dates are typed."""
    vehicle = get_object_or_404(FleetVehicle, pk=pk)
    starts_on = parse_date(request.GET.get("from") or "")
    if starts_on is None:
        return JsonResponse({"success": False, "error": "Pick a start date."}, status=400)
    back = parse_date(request.GET.get("back") or "")
    if back is not None and back <= starts_on:
        return JsonResponse(
            {"success": False, "error": "The return date has to be after the start date."},
            status=400)
    try:
        ignore = int(request.GET.get("ignore") or 0) or None
    except ValueError:
        ignore = None

    units = fleet_capacity.fleet_units()
    unit = next((u for u in units if u.id == vehicle.id), vehicle)
    result = fleet_capacity.check_window(unit, starts_on, back, units, ignore_downtime_id=ignore)
    return JsonResponse({"success": True, **_check_payload(result)})


def _check_payload(result):
    return {
        "level": result["level"],
        "caused": result.get("caused", False),
        "summary": result["summary"],
        "days": [{
            "date": r["date"].isoformat(),
            "label": r["date"].strftime("%a %d"),
            "level": r["level"],
            "baseline_level": r.get("baseline_level", r["level"]),
            "worsened": r.get("worsened", False),
            "busyness": r["busyness"],
            "peak": r["peak"],
            "peak_at": r["peak_at"],
            "legs": r["legs"],
            "available": r["available"],
            "typical": r["typical_units"],
            "reasons": r["reasons"],
        } for r in result["days"]],
    }


def _compact_snapshot(result):
    """What the verdict rested on, small enough to keep on the row."""
    return {
        "summary": result["summary"],
        "days": [{
            "date": r["date"].isoformat(), "level": r["level"], "peak": r["peak"],
            "legs": r["legs"], "available": r["available"], "typical": r["typical_units"],
        } for r in result["days"]],
    }


# ════════════════════════════════════════════════════════════════════════════
# Downtime — take a unit off the road, bring it back
# ════════════════════════════════════════════════════════════════════════════

def _downtime_fields(data, *, today, existing=None):
    """Validate a downtime payload. Returns (fields, error_sentence)."""
    fields = {}

    category = (data.get("category") or (existing.category if existing else "repair")).strip()
    if category not in {c[0] for c in VehicleDowntime.CATEGORY_CHOICES}:
        return None, "Pick what kind of downtime this is."
    fields["category"] = category

    reason = (data.get("reason") if "reason" in data else (existing.reason if existing else "")) or ""
    reason = reason.strip()[:200]
    if not reason:
        return None, "Say why it's down — dispatch reads this on the board."
    fields["reason"] = reason

    if "vendor" in data or existing is None:
        fields["vendor"] = (data.get("vendor") or "").strip()[:120]
    if "notes" in data or existing is None:
        fields["notes"] = (data.get("notes") or "").strip()

    starts_on, message = _opt_date(data.get("starts_on"), "Start date")
    if message:
        return None, message
    if starts_on is None:
        starts_on = existing.starts_on if existing else today
    fields["starts_on"] = starts_on

    if "expected_back_on" in data:
        back, message = _opt_date(data.get("expected_back_on"), "Expected-back date")
        if message:
            return None, message
    else:
        back = existing.expected_back_on if existing else None
    if back is not None and back <= starts_on:
        return None, "The expected-back date has to be after the start date."
    fields["expected_back_on"] = back
    return fields, None


def _judge_downtime(vehicle, fields, *, ignore_id=None):
    units = fleet_capacity.fleet_units()
    unit = next((u for u in units if u.id == vehicle.id), vehicle)
    return fleet_capacity.check_window(
        unit, fields["starts_on"], fields["expected_back_on"], units,
        ignore_downtime_id=ignore_id)


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_save_downtime(request, pk):
    """
    JSON: take a unit out of service (now or on a future date).

    Runs the demand check first. A window that leaves dispatch SHORT on some
    day comes back as 409 with ``needs_ack`` and the reason, and is saved only
    when the caller sends ``acknowledge: true`` — the founder's rule is that
    the system informs and a person decides, so nothing here refuses outright.
    The verdict is kept on the row so the report can say how often downtime
    landed on a clear day.
    """
    vehicle = get_object_or_404(FleetVehicle.objects.with_open_downtimes(), pk=pk)
    data, error = _body(request)
    if error:
        return error
    today = timezone.localdate()
    fields, message = _downtime_fields(data, today=today)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)

    # Two open windows over the same day would be one downtime with two
    # reasons. Extend the existing one instead.
    for other in vehicle.open_downtimes():
        end = fields["expected_back_on"]
        other_end = other.expected_back_on
        overlap = (other_end is None or fields["starts_on"] < other_end) and \
                  (end is None or other.starts_on < end)
        if overlap and not other.is_overdue(today):
            return JsonResponse({
                "success": False,
                "error": (f"#{vehicle.vehicle_number} already has a downtime covering "
                          f"those dates ({other.label()}). Edit that one instead."),
            }, status=400)

    verdict = _judge_downtime(vehicle, fields)
    # The tick box is for the case where THIS car tips a day into short. A day
    # that is short with every car is dispatch's Saturday, not this decision.
    if verdict["caused"] and not data.get("acknowledge"):
        return JsonResponse({
            "success": False, "needs_ack": True, **_check_payload(verdict),
            "error": verdict["summary"],
        }, status=409)

    issue = None
    if data.get("issue_id"):
        issue = VehicleIssue.objects.filter(pk=data.get("issue_id"), vehicle=vehicle).first()

    downtime = VehicleDowntime.objects.create(
        vehicle=vehicle, created_by=request.user, issue=issue,
        demand_verdict=verdict["level"], demand_snapshot=_compact_snapshot(verdict),
        **fields,
    )
    _touch_planner_cache(fields["starts_on"], fields["expected_back_on"])
    return JsonResponse({
        "success": True, "id": downtime.id, "label": downtime.label(),
        **_check_payload(verdict),
    })


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_update_downtime(request, pk):
    """JSON: change the dates, reason, shop or category of an OPEN downtime.
    Re-judged against the fleet without its old self."""
    downtime = get_object_or_404(VehicleDowntime.objects.select_related("vehicle"), pk=pk)
    if downtime.ended_on is not None:
        return JsonResponse(
            {"success": False, "error": "That downtime is closed — it's history now."},
            status=400)
    data, error = _body(request)
    if error:
        return error
    today = timezone.localdate()
    fields, message = _downtime_fields(data, today=today, existing=downtime)
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)

    vehicle = FleetVehicle.objects.with_open_downtimes().get(pk=downtime.vehicle_id)
    verdict = _judge_downtime(vehicle, fields, ignore_id=downtime.id)
    if verdict["caused"] and not data.get("acknowledge"):
        return JsonResponse({
            "success": False, "needs_ack": True, **_check_payload(verdict),
            "error": verdict["summary"],
        }, status=409)

    old_start, old_back = downtime.starts_on, downtime.expected_back_on
    for key, value in fields.items():
        setattr(downtime, key, value)
    downtime.demand_verdict = verdict["level"]
    downtime.demand_snapshot = _compact_snapshot(verdict)
    downtime.save()
    _touch_planner_cache(min(old_start, fields["starts_on"]),
                         _latest(old_back, fields["expected_back_on"]))
    return JsonResponse({"success": True, "label": downtime.label(), **_check_payload(verdict)})


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_close_downtime(request, pk):
    """
    JSON: the unit is back. ``ended_on`` is the first day it was usable again
    (defaults to today; to the expected date if that has already passed, since
    the car has been on the road since then). Optionally resolves the issue
    that caused it, in the same click.
    """
    downtime = get_object_or_404(VehicleDowntime.objects.select_related("vehicle", "issue"), pk=pk)
    if downtime.ended_on is not None:
        return JsonResponse({"success": False, "error": "Already closed."}, status=400)
    data, error = _body(request)
    if error:
        return error
    today = timezone.localdate()
    ended_on, message = _opt_date(data.get("ended_on"), "Back-on-the-road date")
    if message:
        return JsonResponse({"success": False, "error": message}, status=400)
    if ended_on is None:
        ended_on = today
    if ended_on < downtime.starts_on:
        return JsonResponse(
            {"success": False, "error": "It can't be back before it went down."}, status=400)
    if ended_on > today + timedelta(days=1):
        return JsonResponse(
            {"success": False,
             "error": "That's in the future — push the expected-back date out instead."},
            status=400)

    downtime.ended_on = ended_on
    downtime.closed_by = request.user
    downtime.closed_at = timezone.now()
    note = (data.get("notes") or "").strip()
    if note:
        downtime.notes = (downtime.notes + "\n" if downtime.notes else "") + note
    downtime.save(update_fields=["ended_on", "closed_by", "closed_at", "notes"])

    resolved_issue = False
    if data.get("resolve_issue") and downtime.issue and downtime.issue.resolved_at is None:
        issue = downtime.issue
        issue.resolved_at = timezone.now()
        issue.resolved_by = request.user
        issue.resolution = (data.get("resolution") or note or "Fixed — see downtime.").strip()
        issue.save(update_fields=["resolved_at", "resolved_by", "resolution"])
        resolved_issue = True

    _touch_planner_cache(downtime.starts_on, downtime.expected_back_on)
    return JsonResponse({"success": True, "resolved_issue": resolved_issue,
                         "days_down": downtime.days_down(today)})


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_delete_downtime(request, pk):
    """JSON: remove a downtime entered by mistake. Only while it hasn't
    started (or started today) — once a car has been off the road for days
    the row is history, and history is closed, not deleted."""
    downtime = get_object_or_404(VehicleDowntime, pk=pk)
    today = timezone.localdate()
    if downtime.ended_on is not None or downtime.starts_on < today:
        return JsonResponse({
            "success": False,
            "error": "This downtime has already cost days — close it with the real "
                     "return date instead of deleting it.",
        }, status=400)
    start, back = downtime.starts_on, downtime.expected_back_on
    downtime.delete()
    _touch_planner_cache(start, back)
    return JsonResponse({"success": True})


def _latest(a, b):
    if a is None or b is None:
        return None
    return max(a, b)


def _touch_planner_cache(start, back):
    """The capacity planner caches its heavy pass per date for 60s. A car
    going down (or coming back) should show on the next load, not the one
    after — same courtesy the vehicle-assignment endpoints extend."""
    from django.core.cache import cache

    end = back or (start + timedelta(days=7))
    day = start
    while day < end and (day - start).days < 60:
        cache.delete(f"capacity_planner_{day.isoformat()}")
        day += timedelta(days=1)


# ════════════════════════════════════════════════════════════════════════════
# Reported issues — the dispatch-to-fleet handoff
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_report_issue(request, pk):
    """
    JSON: "something is wrong with this car". Any staff member can file one —
    that is the point; the fleet manager should hear about the grinding noise
    from the system, not from a phone call three days later.

    ``take_down: true`` also opens a downtime starting today (with the same
    reason), for the car that must not go out. The fleet manager is texted
    when alerts are switched on.
    """
    vehicle = get_object_or_404(FleetVehicle.objects.with_open_downtimes(), pk=pk)
    data, error = _body(request)
    if error:
        return error
    title = (data.get("title") or "").strip()[:200]
    if not title:
        return JsonResponse({"success": False, "error": "Say what's wrong, in a line."}, status=400)
    severity = (data.get("severity") or "soon").strip()
    if severity not in {s[0] for s in VehicleIssue.SEVERITY_CHOICES}:
        return JsonResponse({"success": False, "error": "Pick how urgent it is."}, status=400)
    source = (data.get("source") or "dispatch").strip()
    if source not in {s[0] for s in VehicleIssue.SOURCE_CHOICES}:
        source = "dispatch"

    issue = VehicleIssue.objects.create(
        vehicle=vehicle, title=title, details=(data.get("details") or "").strip(),
        severity=severity, source=source, reported_by=request.user,
    )

    downtime = None
    if data.get("take_down"):
        today = timezone.localdate()
        if vehicle.is_out_of_service_on(today):
            downtime = vehicle.downtime_on(today)
        else:
            back, message = _opt_date(data.get("expected_back_on"), "Expected-back date")
            if message:
                return JsonResponse({"success": False, "error": message}, status=400)
            if back is not None and back <= today:
                back = None
            downtime = VehicleDowntime.objects.create(
                vehicle=vehicle, category="repair", reason=title, starts_on=today,
                expected_back_on=back, issue=issue, created_by=request.user,
                demand_verdict="", demand_snapshot=None,
            )
            _touch_planner_cache(today, back)

    notify = fleet_notify.notify_issue_reported(issue)
    return JsonResponse({
        "success": True, "id": issue.id,
        "downtime_id": downtime.id if downtime else None,
        "notified": notify.get("sent", 0),
    })


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_resolve_issue(request, pk):
    """JSON: close a reported issue with what was done."""
    issue = get_object_or_404(VehicleIssue, pk=pk)
    if issue.resolved_at is not None:
        return JsonResponse({"success": False, "error": "Already resolved."}, status=400)
    data, error = _body(request)
    if error:
        return error
    resolution = (data.get("resolution") or "").strip()
    if not resolution:
        return JsonResponse(
            {"success": False, "error": "Say what was done — that's the part worth keeping."},
            status=400)
    issue.resolution = resolution
    issue.resolved_at = timezone.now()
    issue.resolved_by = request.user
    issue.save(update_fields=["resolution", "resolved_at", "resolved_by"])
    return JsonResponse({"success": True})


# ════════════════════════════════════════════════════════════════════════════
# Standard intervals — get the maintenance layer out of its inert state
# ════════════════════════════════════════════════════════════════════════════

# A conservative starting set for a heavily-worked light-duty fleet. These are
# a STARTING POINT, not a manufacturer schedule: the Sprinters' diesel oil
# interval is longer than a Suburban's, and the fleet manager is expected to
# correct each car's row. The baseline is set to TODAY and the current
# odometer, which means "start the clock now" — if the last service is known,
# log it and the interval moves to the real date.
STANDARD_INTERVALS = (
    ("oil", 5_000, 180),
    ("tires", 7_500, None),
    ("brakes", 15_000, None),
    ("inspection", None, 365),
)


def _apply_standard_intervals(vehicle, today):
    """Add the standard intervals this vehicle doesn't already have. Returns
    the service types created."""
    existing = set(
        VehicleServiceSchedule.objects.filter(vehicle=vehicle).values_list("service_type", flat=True)
    )
    odometer = vehicle.odometer_miles
    created = []
    for service_type, miles, days in STANDARD_INTERVALS:
        if service_type in existing:
            continue
        VehicleServiceSchedule.objects.create(
            vehicle=vehicle, service_type=service_type,
            interval_miles=miles, interval_days=days,
            last_done_on=today,
            last_done_odometer_miles=Decimal(odometer) if odometer is not None else None,
            notes="Standard interval. Baseline set to today's odometer — log the real "
                  "last service to correct it.",
        )
        created.append(service_type)
    return created


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_apply_standard_intervals(request, pk):
    vehicle = get_object_or_404(FleetVehicle, pk=pk)
    created = _apply_standard_intervals(vehicle, timezone.localdate())
    return JsonResponse({"success": True, "created": created})


@login_required(login_url="login")
@staff_member_required
@require_POST
def fleet_apply_standard_intervals_all(request):
    """Every active unit gets whichever standard intervals it lacks."""
    today = timezone.localdate()
    touched = {}
    for vehicle in FleetVehicle.objects.filter(is_active=True):
        created = _apply_standard_intervals(vehicle, today)
        if created:
            touched[vehicle.vehicle_number] = created
    return JsonResponse({"success": True, "vehicles": len(touched), "detail": touched})


# ════════════════════════════════════════════════════════════════════════════
# Report — fleet performance over a window
# ════════════════════════════════════════════════════════════════════════════

REPORT_WINDOWS = ((30, "Last 30 days"), (90, "Last 90 days"), (365, "Last 12 months"))


@login_required(login_url="login")
@staff_member_required
def fleet_report(request):
    today = timezone.localdate()
    try:
        days = int(request.GET.get("days") or 30)
    except ValueError:
        days = 30
    if days not in {d for d, _ in REPORT_WINDOWS}:
        days = 30
    end = today
    start = today - timedelta(days=days - 1)
    report = fleet_reporting.build_report(start, end, today)
    context = {
        "fleet_page": "report",
        "days": days,
        "windows": REPORT_WINDOWS,
        "report": report,
        "today": today,
    }
    return render(request, "dispatching/fleet_report.html", context)
