"""Rest Advisor — overnight rest awareness for auto-assign.

Read-only, preview-only. The scorer in `suggest_assignments` already PREVENTS most rest
violations (a soft per-candidate penalty steers the early-morning leg to a rested driver
instead of one who finished late last night). This module VERIFIES the final board: it
catches any working driver whose first pickup still lands without the minimum overnight
rest — including violations the scorer never saw (manual/locked/Build-1st assignments) or
couldn't avoid (he was the only driver of his class). Each card names a rested same-class
alternative when one exists, or says explicitly that none does ("accept, or farm").

Advisory only: no DB writes, no leg moves. The card is informational + dismissible — the
dispatcher sets a later start or accepts. Design: docs/scheduler-automation/rest-advisor-design.md.

Config (SchedulerSettings, MINUTES so the integer-only settings save round-trips 8.5h=510):
  rest_min_gap_minutes  — min rest between prev-day last drop-off and next-day first pickup.
                          0 disables BOTH the scorer penalty and these cards.
  rest_penalty_per_hour — scorer knob only (not used here).
"""
from datetime import datetime, timedelta, date

# Cards only fire for a meaningful deficit (the scorer penalizes any deficit; a card for a
# 5-minute miss would just be noise on the dispatcher's screen).
REST_CARD_GRACE_MIN = 15
REST_MAX_CARDS = 4


def build_rest_advisories(target_date: date, proposed, prev_end_by_driver,
                          working_ids, drivers_by_id, cfg=None):
    """Build rest-violation advisory cards from the FINAL proposed board.

    target_date: the build date.
    proposed: {driver_id: DriverDaySchedule} — the final board (post all passes).
    prev_end_by_driver: {driver_id: datetime of yesterday's last drop-off}. Absent driver
        => no legs yesterday => fully rested (never flagged).
    working_ids: set of driver ids working today (for the same-class alternative search).
    drivers_by_id: {driver_id: Driver} for display names.
    cfg: SchedulerSettings (fetched if None).
    """
    if cfg is None:
        from dispatching.models import SchedulerSettings
        cfg = SchedulerSettings.get_settings()
    min_gap_h = (getattr(cfg, "rest_min_gap_minutes", 0) or 0) / 60.0
    if min_gap_h <= 0 or not prev_end_by_driver:
        return []

    from drivers.models import DriverVehicleAssignment
    from dispatching.scheduler import get_vehicle_tier

    # Each working driver's vehicle CLASS for the date (for the same-tier alternative). Read
    # the raw vehicle_type CharField (e.g. "suv"), NOT str(Vehicle) — get_vehicle_tier keys
    # off the bare type strings in VEHICLE_TIER_ORDER, and the display name ("Suv") is -1.
    vtype_by_driver = {}
    for a in (DriverVehicleAssignment.objects.filter(date=target_date, vehicle__isnull=False)
              .select_related("vehicle", "vehicle__vehicle_type")):
        _vt = getattr(a.vehicle, "vehicle_type", None)
        _raw = getattr(_vt, "vehicle_type", None)
        if _raw:
            vtype_by_driver[a.driver_id] = _raw

    def _rest_at(did, pickup_time):
        """Hours of rest the driver would have at this pickup; None if no prev-day work."""
        pe = prev_end_by_driver.get(did)
        if pe is None:
            return None
        return (datetime.combine(target_date, pickup_time) - pe).total_seconds() / 3600

    # First pickup per working driver on the final board.
    first_pu = {}
    for did, sched in (proposed or {}).items():
        slots = getattr(sched, "slots", None)
        if not slots:
            continue
        first_pu[did] = min(s.pickup_time for s in slots)

    grace_h = REST_CARD_GRACE_MIN / 60.0
    candidates = []
    for did, pu_time in first_pu.items():
        rest_h = _rest_at(did, pu_time)
        if rest_h is None or rest_h >= min_gap_h - grace_h:
            continue   # rested enough (or no prev-day work) — no card
        candidates.append((rest_h, did, pu_time))

    # Worst-rested first; cap the count so the panel never floods.
    candidates.sort(key=lambda t: (t[0], t[1]))
    cards = []
    for rest_h, did, pu_time in candidates[:REST_MAX_CARDS]:
        drv = drivers_by_id.get(did)
        name = str(drv) if drv else f"Driver {did}"
        first_name = name.split()[0] if name else name
        pe = prev_end_by_driver[did]
        my_tier = get_vehicle_tier(vtype_by_driver.get(did, "towncar"))
        tier_label = vtype_by_driver.get(did) or "vehicle"

        # Rested same-class alternative: a working driver of the same vehicle tier who
        # WOULD be rested at this pickup (>= min gap, or didn't work yesterday) — i.e.
        # could have taken the dawn leg instead. Pick the one rested longest.
        alt = None
        alt_best = None
        for o_did in working_ids:
            if o_did == did:
                continue
            if get_vehicle_tier(vtype_by_driver.get(o_did, "towncar")) != my_tier:
                continue
            o_rest = _rest_at(o_did, pu_time)
            rested = (o_rest is None) or (o_rest >= min_gap_h)
            if not rested:
                continue
            # Prefer the most-rested (no prev work sorts as "infinitely rested").
            rank = float("inf") if o_rest is None else o_rest
            if alt_best is None or rank > alt_best:
                alt_best = rank
                o_drv = drivers_by_id.get(o_did)
                o_pe = prev_end_by_driver.get(o_did)
                alt = {"name": str(o_drv) if o_drv else f"Driver {o_did}",
                       "ended": o_pe.strftime("%I:%M %p").lstrip("0") if o_pe else "off yesterday"}

        ended = pe.strftime("%I:%M %p").lstrip("0")
        pu_str = pu_time.strftime("%I:%M %p").lstrip("0")
        rested_by = (pe + timedelta(hours=min_gap_h)).strftime("%I:%M %p").lstrip("0")
        text = (f"{name} ended yesterday {ended} - {pu_str} start is "
                f"{rest_h:.1f}h rest (min {min_gap_h:.1f}h).")
        if alt:
            if alt["ended"] == "off yesterday":
                remedy = f" {alt['name']} ({tier_label}) is fully rested - or push {first_name}'s first leg to {rested_by}+."
            else:
                remedy = f" {alt['name']} ({tier_label}, ended {alt['ended']}) is rested - or push {first_name}'s first leg to {rested_by}+."
        else:
            remedy = f" No rested {tier_label} alternative — accept, or farm the early leg."

        cards.append({
            "signature": f"_rest{did}",
            "kind": "rest",
            "leg_count": 0,
            "driver_id": did,
            "driver_name": name,
            "rest_hours": round(rest_h, 1),
            "min_gap_hours": round(min_gap_h, 1),
            "text": text + remedy,
        })
    return cards
