"""Day Setup — roster + vehicle-plan suggester (read-only).

Proposes, for one date: (a) WHO works — the canonical availability resolver is a HARD gate
(founder: "you gotta make sure they are physically available on the schedule"); per-driver
weekday work-rate then decides who is PRE-CHECKED vs merely listed; (b) WHICH UNIT each
rostered driver takes — per-driver unit affinity + Sprinter certification + tier-coverage
reservation against the day's demanded vehicle types, with explicit swap callouts when a
driver's usual unit is held by someone else.

Design record: docs/scheduler-automation/auto-assign-hour-balancing-design.md PART 4
(adversarially reviewed; the four must-fixes are implemented here and in the apply view).
Everything here is a pure function of (date, DB) — deterministic, no writes. DVA rows are
created only by views.apply_day_setup on the founder's explicit Apply.

Measured calibration (scratch/vehicle_affinity_0609.py, 1,496 rows / 148 dates):
  * availability resolver catches 95.6% of the real crew (necessary filter, hard gate);
  * weekday work-rate >= 0.5 trims the available-but-rarely-works part-timers;
  * affinity is weak overall (only roberto ~90% one-unit) -> dedicated locks for the few
    strong habits, greedy matching for the fluid remainder; copy-yesterday repeats only ~50%.
"""
from datetime import date, timedelta
from math import ceil

# ── Flags ────────────────────────────────────────────────────────────────────
DAY_SETUP_ENABLED = True
DAY_SETUP_HISTORY_DAYS = 90        # affinity/weekday window; ALWAYS bounded to < target_date
DAY_SETUP_WEEKDAY_MIN_RATE = 0.5   # work-rate on this weekday to be PRE-CHECKED
DAY_SETUP_MIN_WEEKDAY_SAMPLES = 3  # fewer same-weekday samples -> pre-check if available
DAY_SETUP_DEDICATED_SHARE = 0.50   # unit share to claim a "his car" dedicated lock (founder:
                                   # full-timers keep semi-permanent cars — george is #005 on
                                   # ~54% of his days, so 0.5 catches all four he named)
DAY_SETUP_DEDICATED_MIN_DAYS = 8   # ...with at least this many days on the unit
DAY_SETUP_MIN_UNIT_DAYS = 5        # units used fewer days than this in the window are
                                   # "rarely used": suggested only as a LAST RESORT (founder:
                                   # "there is no such thing as a car not working today" —
                                   # every unit is fleet capacity, the label just flags it)
DAY_SETUP_SHARE_HANDOFF_HOUR = 15  # default AM/PM split hour for a planned shared car —
                                   # editable per driver in the auto-assign modal afterwards
DAY_SETUP_SHARE_PAD_HOURS = 0      # AM partner's End and PM partner's Start are the handoff
                                   # hour itself; the engine's hard pickup-hour windows +
                                   # clear-by guard keep the physical handoff honest
DAY_SETUP_LEGS_PER_UNIT = 6        # tier reservation: ceil(tier_legs / this) units reserved
DAY_SETUP_YESTERDAY_BONUS = 40     # keep-the-same-car continuity: strong enough to beat a
                                   # moderate affinity for a different unit (share*100 scale),
                                   # but a returning regular's dedicated lock still wins —
                                   # P1 claims units before this bonus is ever scored
                                   # (founder: "george off Tuesday gets his car back Wednesday
                                   # even though someone else had it yesterday")
DAY_SETUP_EXCLUDE_DRIVER_IDS = {6}  # placeholder; demo accounts also name-guarded below

_DEMO_NAME_MARKERS = ("demo", "placeholder", "test account")


def _is_excluded(driver):
    if driver.id in DAY_SETUP_EXCLUDE_DRIVER_IDS:
        return True
    name = str(driver).lower()
    if driver.profile:
        name += " " + (driver.profile.first_name or "").lower()
    return any(m in name for m in _DEMO_NAME_MARKERS)


def _unit_sort_key(unit):
    """Numeric-aware unit ordering ('015' must not sort before '10')."""
    num = (unit.vehicle_number or "").strip()
    try:
        return (0, int(num), num)
    except ValueError:
        return (1, 0, num)


def _unit_label(unit):
    vt = str(unit.vehicle_type) if unit.vehicle_type else ""
    return f"#{unit.vehicle_number}" + (f" {vt}" if vt else "")


def _unit_tier(unit):
    """Tier index of a FleetVehicle. NOTE: rates.Vehicle.__str__ returns .title()-cased
    text ('Suv') which does NOT match VEHICLE_TIER_ORDER — always read the raw
    vehicle_type CharField, same as scheduler.load_all_driver_vtypes."""
    from dispatching.scheduler import get_vehicle_tier
    if unit is None or unit.vehicle_type is None:
        return -1
    return get_vehicle_tier(unit.vehicle_type.vehicle_type)


def suggest_day_setup(target_date: date, ignore_existing: bool = False) -> dict:
    """Build the Day Setup proposal for `target_date`. Read-only, deterministic.

    ignore_existing=True is for backtesting only: pretend the date has no DVA rows so the
    proposal can be scored against what the founder actually built that day.
    """
    from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
    from reservations.models import Leg
    from dispatching.scheduler import get_vehicle_tier

    history_start = target_date - timedelta(days=DAY_SETUP_HISTORY_DAYS)

    # ── History (STRICTLY date < target_date: the founder pre-builds days ahead, and
    # future rows must never leak into the stats or the backtest) ──
    hist = list(
        DriverVehicleAssignment.objects
        .filter(date__gte=history_start, date__lt=target_date, vehicle__isnull=False)
        .values_list("driver_id", "vehicle_id", "date")
    )
    worked_days = {}          # driver -> set of dates
    unit_days = {}            # (driver, vehicle) -> day count
    unit_used_days = {}       # vehicle -> set of dates
    last_unit = {}            # driver -> (date, vehicle_id), most recent
    for did, vid, d in hist:
        worked_days.setdefault(did, set()).add(d)
        unit_days[(did, vid)] = unit_days.get((did, vid), 0) + 1
        unit_used_days.setdefault(vid, set()).add(d)
        if did not in last_unit or d > last_unit[did][0]:
            last_unit[did] = (d, vid)
    operating_dates = sorted({d for _, _, d in hist})
    same_weekday_ops = [d for d in operating_dates if d.weekday() == target_date.weekday()]

    # ── Roster candidates ──
    drivers = list(
        Driver.objects.filter(driver_type="inhouse", is_active=True)
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides",
                          "preferred_vehicles", "preferred_vehicle_types",
                          "certified_vehicle_types")
    )
    drivers = sorted((d for d in drivers if not _is_excluded(d)), key=lambda d: d.id)

    existing = {}
    stale_rows = []
    if not ignore_existing:
        for a in (DriverVehicleAssignment.objects.filter(date=target_date)
                  .select_related("vehicle", "vehicle__vehicle_type",
                                  "driver", "driver__profile")):
            # A row held by a DEACTIVATED/excluded driver is stale data, not a worker:
            # it must neither render as crew nor lock its unit out of the pool
            # (e.g. neuma/shipo, deactivated, still holding #003/#009 on old boards).
            if (not a.driver.is_active or a.driver.driver_type != "inhouse"
                    or _is_excluded(a.driver)):
                if a.vehicle_id:
                    stale_rows.append(f"{a.driver} (inactive) still holds "
                                      f"{_unit_label(a.vehicle)} — treated as free; "
                                      f"clear the row in the vehicle panel.")
                continue
            existing[a.driver_id] = a

    # ── Demand by tier (Leg.vehicle is NEVER populated — tier comes from
    # reservation.vehicle via effective_vehicle_type) ──
    legs = (
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled").exclude(status="cancelled")
        .select_related("reservation__vehicle", "vehicle")
    )
    demand = {}
    for leg in legs:
        vt = leg.effective_vehicle_type
        if vt:
            demand[str(vt)] = demand.get(str(vt), 0) + 1

    # ── Units ──
    all_units = sorted(
        FleetVehicle.objects.filter(is_active=True).select_related("vehicle_type"), key=_unit_sort_key)
    rarely_used = {u.id for u in all_units
                   if len(unit_used_days.get(u.id, ())) < DAY_SETUP_MIN_UNIT_DAYS}
    locked_unit_ids = {a.vehicle_id for a in existing.values() if a.vehicle_id}
    free_units = [u for u in all_units if u.id not in locked_unit_ids]

    # ── Classify drivers ──
    rows, warnings, swaps = [], list(stale_rows), []
    assignable = []   # (order_score, driver) for the matching phases, pre-checked only
    avail_start = {}  # checked drivers' availability start hour (share pass: who's early crew)
    for d in drivers:
        if d.id in existing:
            a = existing[d.id]
            rows.append({
                "driver_id": d.id, "driver_name": str(d), "group": "locked",
                "checked": True,
                "vehicle_id": a.vehicle_id,
                "vehicle_label": _unit_label(a.vehicle) if a.vehicle else "(no unit)",
                "reason": "already set", "hint": "",
            })
            if not a.vehicle_id:
                warnings.append(f"{d} has a vehicle row with NO unit — fix in the panel.")
            continue

        eff = d.get_effective_availability(target_date)
        if not eff["is_available"]:
            rows.append({
                "driver_id": d.id, "driver_name": str(d), "group": "off",
                "checked": False, "vehicle_id": None, "vehicle_label": "",
                "reason": "schedule says OFF", "hint": eff.get("tooltip", "") or "",
            })
            continue

        wd = worked_days.get(d.id, set())
        wd_same = [x for x in wd if x.weekday() == target_date.weekday()]
        # Weekday-rate denominator starts at the driver's FIRST appearance in the window —
        # a recent hire (e.g. ~37 days of history) must not be diluted by operating days
        # that predate them, or they get demoted on every single date.
        first_seen = min(wd) if wd else None
        eligible_ops = [x for x in same_weekday_ops if first_seen is None or x >= first_seen]
        samples = len(eligible_ops)
        rate = (len(wd_same) / samples) if samples else None
        prev_op = operating_dates[-1] if operating_dates else None
        worked_prev = prev_op is not None and prev_op in wd
        if samples < DAY_SETUP_MIN_WEEKDAY_SAMPLES or rate is None:
            checked, hint = True, "available (new/limited history)"
        elif rate >= DAY_SETUP_WEEKDAY_MIN_RATE:
            checked = True
            hint = f"works {len(wd_same)}/{samples} recent {target_date.strftime('%A')}s"
        elif worked_prev:
            # Active streak: the day-to-day roster repeats ~75%, so someone who worked the
            # last operating day belongs in the crew even on an unusual weekday for them.
            checked = True
            hint = f"worked {prev_op.month}/{prev_op.day} (active streak)"
        else:
            checked = False
            hint = f"only {len(wd_same)}/{samples} recent {target_date.strftime('%A')}s"
        rows.append({
            "driver_id": d.id, "driver_name": str(d),
            "group": "suggested" if checked else "available",
            "checked": checked, "vehicle_id": None, "vehicle_label": "",
            "reason": "", "hint": hint,
        })
        if checked:
            assignable.append(d)
            avail_start[d.id] = eff["start_hour"] if eff["start_hour"] is not None else 4

    # ── Vehicle matching over the pre-checked, unlocked drivers ──
    by_id = {d.id: d for d in drivers}
    n_days = {d.id: len(worked_days.get(d.id, ())) for d in drivers}

    def share(did, vid):
        nd = n_days.get(did, 0)
        return (unit_days.get((did, vid), 0) / nd) if nd else 0.0

    def pair_score(d, u):
        s = share(d.id, u.id) * 100.0
        lu = last_unit.get(d.id)
        if lu and lu[1] == u.id:
            s += DAY_SETUP_YESTERDAY_BONUS
        if any(pv.id == u.id for pv in d.preferred_vehicles.all()):
            s += 10.0
        if u.vehicle_type and any(pt.pk == u.vehicle_type.pk
                                  for pt in d.preferred_vehicle_types.all()):
            s += 5.0
        return s

    proposed = {}          # driver_id -> FleetVehicle
    taken = set(locked_unit_ids)
    pool = {u.id: u for u in free_units}
    unmatched = list(assignable)

    def assign(d, u, reason):
        proposed[d.id] = u
        taken.add(u.id)
        pool.pop(u.id, None)
        for r in rows:
            if r["driver_id"] == d.id:
                r["vehicle_id"] = u.id
                r["vehicle_label"] = _unit_label(u)
                r["reason"] = reason
        if d in unmatched:
            unmatched.remove(d)

    # P1 — dedicated locks ("his car"). Founder rule: full-timers (george #005, David #008,
    # roberto #004, sereen #003) keep semi-permanent cars — never suggest them another unit.
    # Two sources, strongest first: (a) an EXPLICIT admin-set Driver.preferred_vehicles unit
    # (the founder's direct control — sorts ahead via share 1.01), then (b) history (top-unit
    # share >= DAY_SETUP_DEDICATED_SHARE over >= MIN_DAYS).
    dedicated = []
    for d in assignable:
        pref_units = sorted(d.preferred_vehicles.all(), key=_unit_sort_key)
        if pref_units:
            dedicated.append((-1.01, d.id, d, pref_units[0].id, True))
            continue
        best_vid, best_share = None, 0.0
        for (did, vid), cnt in unit_days.items():
            if did == d.id:
                s = share(did, vid)
                if s > best_share:
                    best_vid, best_share = vid, s
        if (best_vid is not None and best_share >= DAY_SETUP_DEDICATED_SHARE
                and unit_days.get((d.id, best_vid), 0) >= DAY_SETUP_DEDICATED_MIN_DAYS):
            dedicated.append((-best_share, d.id, d, best_vid, False))
    for _, _, d, vid, explicit in sorted(dedicated):
        u = pool.get(vid)
        if u is None:
            holder = next((str(by_id[od]) for od, a in existing.items()
                           if a.vehicle_id == vid), None)
            holder = holder or next((str(by_id[od]) for od, pu in proposed.items()
                                     if pu.id == vid), "someone else")
            swaps.append(f"{d} usually drives #{_vnum(vid, all_units)} — {holder} holds it "
                         f"today; {d} gets the next-best fit.")
            continue
        if d.can_drive(u.vehicle_type):
            assign(d, u, "his car (set in admin)" if explicit
                   else f"usual unit · {share(d.id, u.id):.0%}")
            # Handback callout: someone else drove this regular's car most recently (e.g.
            # while he was off) — tell the dispatcher why that driver gets a different unit.
            for other in assignable:
                if other.id != d.id and last_unit.get(other.id, (None, None))[1] == u.id:
                    swaps.append(f"#{u.vehicle_number} goes back to {d} (his car) — "
                                 f"{other} drove it last; {other} gets another unit.")
                    break

    # P2 — tier RESERVATION (must-fix: reserve, don't just warn). Highest tier first;
    # locked + already-proposed units count toward coverage.
    tiers_desc = sorted(demand.keys(), key=lambda t: -get_vehicle_tier(t))
    for tname in tiers_desc:
        tier = get_vehicle_tier(tname)
        if tier < 0 or not demand[tname]:
            continue
        need = ceil(demand[tname] / DAY_SETUP_LEGS_PER_UNIT)
        covered = 0
        for a in existing.values():
            if a.vehicle is not None and _unit_tier(a.vehicle) >= tier:
                covered += 1
        for u in proposed.values():
            if _unit_tier(u) >= tier:
                covered += 1
        while covered < need:
            # exact-tier free units first, then higher; rarely-used units last (not banned —
            # founder: every car is working capacity).
            candidates = sorted(
                (u for u in pool.values() if _unit_tier(u) >= tier),
                key=lambda u: (u.id in rarely_used, _unit_tier(u), _unit_sort_key(u)))
            placed = False
            for u in candidates:
                takers = sorted(
                    (d for d in unmatched if d.can_drive(u.vehicle_type)),
                    key=lambda d: (-pair_score(d, u), d.id))
                if takers:
                    assign(takers[0], u,
                           f"covers {tname} demand ({demand[tname]} legs)")
                    covered += 1
                    placed = True
                    break
            if not placed:
                warnings.append(
                    f"{tname} demand ({demand[tname]} legs) needs {need} unit(s) of that "
                    f"size — only {covered} staffed. Add a certified driver or free a unit.")
                break

    # P3 — fluid remainder: best-scoring pair first, deterministic; rarely-used units are a
    # last resort (sorted after every normal pairing), never banned.
    while unmatched:
        best = None
        for d in unmatched:
            for u in pool.values():
                if not d.can_drive(u.vehicle_type):
                    continue
                key = (u.id in rarely_used, -pair_score(d, u), d.id) + _unit_sort_key(u)
                if best is None or key < best[0]:
                    best = (key, d, u)
        if best is None:
            break
        _, d, u = best
        s = share(d.id, u.id)
        lu = last_unit.get(d.id)
        if lu and lu[1] == u.id:
            reason = "same car as last shift"
        elif s >= 0.3:
            reason = f"usual unit · {s:.0%}"
        else:
            reason = "best fit"
        assign(d, u, reason)

    # P3c — PLANNED SHARED CARS. More checked drivers than free cars (founder: "we have
    # drivers available, more than cars — and there is no such thing as a car not working"):
    # instead of leaving a checked driver carless (his colleagues then run 15h+ days), pair
    # him onto the EARLIEST starter's unit as the PM shift. Both rows get partitioned
    # planned windows (AM: start→handoff, PM: handoff→23) which Apply persists onto the
    # vehicle rows; the auto-assign modal prefills them as HARD windows, so the engine
    # physically cannot double-book the car. Partners come only from THIS run's proposals —
    # rows the founder already set stay untouched.
    if unmatched:
        sharable = sorted(((avail_start.get(pid, 4), pid) for pid in proposed),
                          key=lambda t: (t[0], t[1]))
        shared_partners = set()
        h = DAY_SETUP_SHARE_HANDOFF_HOUR
        for d in list(unmatched):
            pick = None
            for st, pid in sharable:
                if pid in shared_partners:
                    continue
                u = proposed[pid]
                if d.can_drive(u.vehicle_type):
                    pick = (st, pid, u)
                    break
            if pick is None:
                continue
            st, pid, u = pick
            shared_partners.add(pid)
            partner = by_id[pid]
            for r in rows:
                if r["driver_id"] == pid:
                    r["planned_start_hour"] = int(st)
                    # End is a LAST-PICKUP bound: h-1 means pickups stop at (h-1):59, so the
                    # AM partner's final job clears around the handoff instead of after it.
                    # The engine's shared-car occupancy gate is the hard backstop either way.
                    r["planned_end_hour"] = h - 1
                    r["share"] = {"partner": str(d), "role": "AM", "until": h}
                    r["reason"] = (r["reason"] + " · " if r.get("reason") else "") + \
                                  f"until {h}:00, then hands off"
                elif r["driver_id"] == d.id:
                    r["vehicle_id"] = u.id
                    r["vehicle_label"] = _unit_label(u)
                    r["planned_start_hour"] = h
                    r["planned_end_hour"] = 23
                    r["share"] = {"partner": str(partner), "role": "PM", "from": h}
                    r["reason"] = f"takes {_unit_label(u)} from {partner} at {h}:00"
            swaps.append(f"SHARED CAR: {partner} drives {_unit_label(u)} until {h}:00, "
                         f"then {d} takes it for the evening — both days stay short.")
            unmatched.remove(d)

    # P3b — PREFILL for unchecked-but-available drivers (founder: "why no car next to
    # Raymond/shelley?"). Not a reservation — the unit stays parked and in free_units;
    # this only presets each row's dropdown so ticking the checkbox is one click.
    # Globally best pair first; one prefill per unit so two rows never collide.
    prefill_rows = [r for r in rows if r["group"] == "available" and not r["vehicle_id"]]
    prefill_units = dict(pool)
    while prefill_rows and prefill_units:
        best = None
        for r in prefill_rows:
            d = by_id.get(r["driver_id"])
            if d is None:
                continue
            for u in prefill_units.values():
                if not d.can_drive(u.vehicle_type):
                    continue
                key = (u.id in rarely_used, -pair_score(d, u), d.id) + _unit_sort_key(u)
                if best is None or key < best[0]:
                    best = (key, r, d, u)
        if best is None:
            break
        _, r, d, u = best
        r["vehicle_id"] = u.id
        r["vehicle_label"] = _unit_label(u)
        r["reason"] = "if working — best fit"
        prefill_rows.remove(r)
        prefill_units.pop(u.id)

    # P4 — leftovers.
    for d in unmatched:
        warnings.append(f"No free unit for {d} — uncheck them or clear a vehicle.")
        for r in rows:
            if r["driver_id"] == d.id:
                r["reason"] = "no unit free"
    parked = [u for u in pool.values() if u.id not in rarely_used]
    if parked:
        warnings.append("Parked (unassigned) units: "
                        + ", ".join(_unit_label(u) for u in sorted(parked, key=_unit_sort_key)))
    rare_listed = [u for u in pool.values() if u.id in rarely_used]
    if rare_listed:
        warnings.append("Rarely-used units (never auto-suggested): "
                        + ", ".join(_unit_label(u) for u in sorted(rare_listed, key=_unit_sort_key)))

    group_order = {"locked": 0, "suggested": 1, "available": 2, "off": 3}
    rows.sort(key=lambda r: (group_order[r["group"]], r["driver_name"].lower()))
    return {
        "date": target_date.isoformat(),
        "rows": rows,
        "swaps": swaps,
        "warnings": warnings,
        "demand": dict(sorted(demand.items(), key=lambda kv: -get_vehicle_tier(kv[0]))),
        "free_units": [
            {"id": u.id, "label": _unit_label(u),
             "requires_cert": bool(u.vehicle_type and u.vehicle_type.requires_certification),
             "rarely_used": u.id in rarely_used}
            for u in sorted(pool.values(), key=_unit_sort_key)
        ],
        # snapshot for the apply view's drift check (409 if a row changed since preview)
        "snapshot": {str(a.driver_id): a.vehicle_id for a in existing.values()},
    }


def _vnum(vid, units):
    for u in units:
        if u.id == vid:
            return u.vehicle_number
    return "?"
