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
# The default AM/PM split hour for a planned shared car moved to the live
# SchedulerSettings singleton (`share_split_hour`, default 16 — the measured
# modal handoff hour; scheduling redesign Build 2e). Still editable per driver
# in the auto-assign modal afterwards.
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
DAY_SETUP_SOLO_FIRST = True        # drivers > cars: leave the extras UNCHECKED ("available —
                                   # add via Advisor if the day needs them") instead of auto-
                                   # proposing an AM/PM share. The Second-Shift Advisor, which
                                   # reads the actual built board, proposes the split only when
                                   # the day truly needs it. False = legacy auto-share (P3c).
DAY_SETUP_PEAK_SIZING = True       # size the checked roster by PEAK CONCURRENT in-flight
                                   # demand (the histogram), NOT daily totals — founder rule
                                   # from the 06-01 drive ("13 legs in flight at 09:30 ->
                                   # 13 drivers was right"; never naive legs-per-driver).
DAY_SETUP_PEAK_BUFFER = 1          # bodies above the in-flight peak: the peak is a LOWER
                                   # bound (turnaround/deadhead means a driver can't always
                                   # chain in-flight-adjacent legs); +1 reproduces the
                                   # founder's 06-01 answer (measured 12 -> 13 checked).

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


def peak_concurrency(target_date, legs=None):
    """In-flight concurrency histogram for a date — the founder's roster-sizing measure.

    Returns {"overall": (n, datetime), "per_tier": {tier: (n, datetime)} (exact-tier,
    the founder's counting, for callouts), "cumulative": {tier: (n, datetime)} (peak of
    in-flight legs with tier >= t — the CORRECT coverage measure under nested vehicle
    compatibility: 2 van + 1 Van14 overlapping need 3 van-capable units, not 2 and 1),
    "total_legs": int}.

    Leg exclusions are identical to the Day Setup demand query (cancelled leg or
    reservation); unpaid legs COUNT (demand parity — Day Setup has no skip-unpaid
    concept, and staffing for a maybe-paid leg errs safe; the UI's exclude_unpaid only
    lowers COVERAGE numbers, see the ROADMAP gotcha). Untiered legs count in `overall`
    only (a body is still needed). End times via estimate_job_end_time — same estimator
    the planner uses; the timing cache is preloaded if cold (one query) so a suggest
    click never pays per-leg DB fallbacks.
    """
    from datetime import datetime as _dt
    import dispatching.scheduler as sch
    from dispatching.scheduler import get_vehicle_tier
    if legs is None:
        from reservations.models import Leg
        legs = list(Leg.objects.filter(pickup_date=target_date)
                    # BOTH cancellation spellings exist on Reservation.status in
                    # production ('cancelled' and one-L 'canceled'); Leg.status only
                    # ever carries the two-L form. (00 §A6 non-negotiable filter.)
                    .exclude(reservation__status__in=("cancelled", "canceled"))
                    .exclude(status="cancelled")
                    .select_related("reservation__vehicle", "vehicle",
                                    "reservation", "flight_information"))
    if sch._timing_cache is None:
        sch.preload_timing_cache()
    events = []
    for leg in legs:
        vt = leg.effective_vehicle_type
        events.append((_dt.combine(target_date, leg.pickup_time), 1, str(vt) if vt else None))
        events.append((sch.estimate_job_end_time(leg, target_date), -1, str(vt) if vt else None))
    # arrivals before departures at ties — the conservative reading that reproduced the
    # founder's 06-01 peak (12 @ 09:30 measured vs his 13 read, same moment).
    events.sort(key=lambda e: (e[0], -e[1]))
    overall_peak, overall_at, overall_cur = 0, None, 0
    tier_cur, tier_peak = {}, {}
    cum_cur, cum_peak = {}, {}
    tiers_present = sorted({t for _, _, t in events if t}, key=get_vehicle_tier)
    for t, d, tname in events:
        overall_cur += d
        if overall_cur > overall_peak:
            overall_peak, overall_at = overall_cur, t
        if tname:
            tier_cur[tname] = tier_cur.get(tname, 0) + d
            if tier_cur[tname] > tier_peak.get(tname, (0, None))[0]:
                tier_peak[tname] = (tier_cur[tname], t)
            k = get_vehicle_tier(tname)
            for ct in tiers_present:
                if get_vehicle_tier(ct) <= k:
                    cum_cur[ct] = cum_cur.get(ct, 0) + d
                    if cum_cur[ct] > cum_peak.get(ct, (0, None))[0]:
                        cum_peak[ct] = (cum_cur[ct], t)
    return {"overall": (overall_peak, overall_at), "per_tier": tier_peak,
            "cumulative": cum_peak, "total_legs": len(legs)}


def concurrency_series(target_date, legs, step_minutes=30):
    """Hour-by-hour in-flight concurrency, split by vehicle type — the picture behind
    `peak_concurrency`'s single number.

    Returns [{"t": "HH:MM", "n": int, "tiers": {vtype: int}}] on a `step_minutes` grid
    covering only the hours that actually carry work (plus one empty slot either side so
    the chart doesn't start mid-bar). Same leg set and same end-time estimator as
    peak_concurrency, so the series and the peak can never disagree.
    """
    from datetime import datetime as _dt, timedelta as _td
    import dispatching.scheduler as sch
    if not legs:
        return []
    if sch._timing_cache is None:
        sch.preload_timing_cache()
    spans = []
    for leg in legs:
        vt = leg.effective_vehicle_type
        spans.append((_dt.combine(target_date, leg.pickup_time),
                      sch.estimate_job_end_time(leg, target_date),
                      str(vt) if vt else None))
    first = min(s for s, _e, _v in spans)
    last = max(e for _s, e, _v in spans)
    step = _td(minutes=step_minutes)
    # snap the start down to the grid, then pad one slot each side
    base = first.replace(minute=(first.minute // step_minutes) * step_minutes,
                         second=0, microsecond=0) - step
    out = []
    t = base
    while t <= last + step:
        tiers = {}
        for s, e, v in spans:
            if s <= t < e and v:
                tiers[v] = tiers.get(v, 0) + 1
        out.append({"t": t.strftime("%H:%M"),
                    "n": sum(1 for s, e, _v in spans if s <= t < e),
                    "tiers": tiers})
        t += step
    return out


def parkable_units(units, cumulative, overall=0):
    """Which cars can stay in the yard today and still leave every trip covered.

    Founder's model, and the reason per-size demand targets are the wrong question: an
    SUV covers SUV work AND everything below it, so on a normal day every car goes out
    and size never binds. Size only matters on a genuinely quiet day, where the question
    is not "how many of each size do I need" but "can anything stay in".

    Under nested compatibility the fleet covers the day exactly when, for EVERY size S,
    the cars of size >= S outnumber the peak concurrent trips needing size >= S — Hall's
    condition, and `cumulative` (from peak_concurrency) is already that left-hand side.
    Park the LEAST capable car first: it satisfies the fewest constraints, so releasing
    it costs the least coverage. Stop at the first car that would break the condition.

    `overall` is the peak concurrent trip count of ANY type and acts as a floor: a leg
    with no vehicle type on its reservation still needs a body, and it appears in the
    overall peak but in NO tier — without the floor, a day of purely untyped legs has an
    empty `cumulative`, every per-size test passes vacuously, and the whole fleet gets
    declared parkable.

    Returns (parked, staffed) — both lists of FleetVehicle, parked in park order.
    Measured against 21 built days: mean error 0.57 cars, and on the 10 days the founder
    used every car it agreed 9 times (it errs toward sending cars OUT, the safe side).
    """
    from dispatching.scheduler import get_vehicle_tier
    staffed = sorted(units, key=_unit_tier)      # least capable first
    parked = []
    while len(staffed) > overall:
        trial = staffed[1:]
        covers = all(
            sum(1 for u in trial if _unit_tier(u) >= get_vehicle_tier(tname)) >= need
            for tname, (need, _at) in cumulative.items()
        )
        if not covers:
            break
        parked.append(staffed[0])
        staffed = trial
    return parked, staffed


def suggest_day_setup(target_date: date, ignore_existing: bool = False,
                      solo_first=None, peak_sizing=None,
                      force_include=None, force_exclude=None) -> dict:
    """Build the Day Setup proposal for `target_date`. Read-only, deterministic.

    ignore_existing=True is for backtesting only: pretend the date has no DVA rows so the
    proposal can be scored against what the founder actually built that day.
    solo_first / peak_sizing: None resolves to the module flag at call time; pass an
    explicit bool to A/B per request.
    force_include / force_exclude: driver ids the dispatcher ticked/unticked before a
    re-suggest ("Yovanny in, someone out"). Forced drivers bypass the rate/streak gate
    (availability stays HARD), rank top everywhere, and displace the lowest-priority
    proposal if cars run out (P3d). Excluded drivers stay unchecked.
    """
    _solo = DAY_SETUP_SOLO_FIRST if solo_first is None else bool(solo_first)
    _peak = DAY_SETUP_PEAK_SIZING if peak_sizing is None else bool(peak_sizing)
    forced = {int(x) for x in (force_include or [])}
    f_excl = {int(x) for x in (force_exclude or [])} - forced
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
    # reservation.vehicle via effective_vehicle_type). Materialized once; shared with
    # the peak-concurrency histogram (estimate_job_end_time touches reservation +
    # flight_information, so select them here and avoid the N+1).
    legs = list(
        Leg.objects.filter(pickup_date=target_date)
        # Same exclusion as peak_concurrency above (the docstring there promises
        # parity): both Reservation cancellation spellings, two-L Leg status.
        .exclude(reservation__status__in=("cancelled", "canceled"))
        .exclude(status="cancelled")
        .select_related("reservation__vehicle", "vehicle",
                        "reservation", "flight_information")
    )
    demand = {}
    for leg in legs:
        vt = leg.effective_vehicle_type
        if vt:
            demand[str(vt)] = demand.get(str(vt), 0) + 1

    # ── Units ──
    # "There is no such thing as a car not working today" (the DAY_SETUP_MIN_UNIT_DAYS
    # note above) is about RARELY-USED units: those are still fleet capacity and only
    # get a label. An out-of-service unit is the one genuine exception — a human put
    # it on a lift and said so, with a date window. Excluded from the proposal
    # entirely rather than ranked last, because proposing a car that is physically in
    # a shop isn't a weaker suggestion, it's a wrong one. Named in `warnings` so the
    # dispatcher sees WHY the fleet looks a unit short today.
    all_units = sorted(
        FleetVehicle.objects.filter(is_active=True).select_related("vehicle_type"), key=_unit_sort_key)
    oos_units = [u for u in all_units if u.is_out_of_service_on(target_date)]
    all_units = [u for u in all_units if u not in oos_units]
    oos_warnings = [
        f"{_unit_label(u)} is out of service — {u.out_of_service_label(target_date)}; "
        f"not proposed today."
        for u in oos_units
    ]
    rarely_used = {u.id for u in all_units
                   if len(unit_used_days.get(u.id, ())) < DAY_SETUP_MIN_UNIT_DAYS}
    locked_unit_ids = {a.vehicle_id for a in existing.values() if a.vehicle_id}
    free_units = [u for u in all_units if u.id not in locked_unit_ids]

    # ── Classify drivers ──
    rows, warnings, swaps = [], list(stale_rows) + oos_warnings, []
    assignable = []   # (order_score, driver) for the matching phases, pre-checked only
    avail_start = {}  # checked drivers' availability start hour (share pass: who's early crew)
    rank_of = {}      # driver_id -> priority tuple (peak cap + P3d displacement order)
    _known_ids = {d.id for d in drivers}
    for fid in sorted(forced - _known_ids):
        warnings.append(f"Force-included driver id {fid} is unknown/inactive/excluded — "
                        f"ignored.")
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
            if d.id in forced:
                # Availability is the founder's HARD gate — force-include never overrides
                # the schedule. The Advisor path can suggest OFF drivers, loudly labeled.
                warnings.append(
                    f"{d} is in your picks but the schedule says OFF — Day Setup never "
                    f"overrides the schedule; fix the schedule or add via the "
                    f"Second-Shift Advisor (it suggests OFF drivers, labeled).")
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
        if d.id in f_excl:
            checked, hint = False, "unchecked by you"
        elif d.id in forced:
            checked, hint = True, "included by you"
        elif samples < DAY_SETUP_MIN_WEEKDAY_SAMPLES or rate is None:
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
        # Rank: who stays when the peak cap or P3d displacement needs a "lowest priority".
        # Rates rank WHO; the peak sizes HOW MANY. Limited-history drivers sit exactly at
        # the threshold (benefit of the doubt, below every established regular).
        rank_of[d.id] = ((rate if (samples >= DAY_SETUP_MIN_WEEKDAY_SAMPLES
                                   and rate is not None)
                          else DAY_SETUP_WEEKDAY_MIN_RATE),
                         1 if worked_prev else 0, len(wd_same), -d.id)
        rows.append({
            "driver_id": d.id, "driver_name": str(d),
            "group": "suggested" if checked else "available",
            "checked": checked, "vehicle_id": None, "vehicle_label": "",
            "reason": "", "hint": hint,
            "forced": d.id in forced,
        })
        if checked:
            assignable.append(d)
            avail_start[d.id] = eff["start_hour"] if eff["start_hour"] is not None else 4

    # ── PEAK-CONCURRENCY ROSTER SIZING (founder rule: size by the in-flight histogram,
    # never by daily totals or naive legs-per-driver). Rates ranked WHO above; the peak
    # decides HOW MANY: drivers beyond peak+buffer step down to "available" (unchecked,
    # one-click re-add via P3b prefill). Locked rows and your forced picks always stay.
    # Cert guard: never drop a driver if it would leave fewer certified bodies than a
    # certification tier's cumulative in-flight peak (the Sprinter case).
    peak = None
    if _peak and legs:
        peak = peak_concurrency(target_date, legs=legs)
        n_target = peak["overall"][0] + DAY_SETUP_PEAK_BUFFER
        cert_tiers = {}
        for u in all_units:
            vt = u.vehicle_type
            if vt is not None and getattr(vt, "requires_certification", False):
                req = peak["cumulative"].get(vt.vehicle_type, (0, None))[0]
                if req:
                    cert_tiers[vt.vehicle_type] = req
        cert_of = {d.id: {v.vehicle_type for v in d.certified_vehicle_types.all()}
                   for d in drivers}
        checked_rows = [r for r in rows if r["checked"]]
        kept_cert = {t: sum(1 for r in checked_rows
                            if t in cert_of.get(r["driver_id"], ()))
                     for t in cert_tiers}
        checked_n = len(checked_rows)
        droppable = sorted((r for r in rows
                            if r["group"] == "suggested" and r["checked"]
                            and not r.get("forced")),
                           key=lambda r: rank_of.get(r["driver_id"], (0.0, 0, 0, 0)))
        dropped_names = []
        for r in droppable:
            if checked_n <= n_target:
                break
            did = r["driver_id"]
            if any(t in cert_of.get(did, ()) and kept_cert[t] - 1 < req
                   for t, req in cert_tiers.items()):
                continue   # dropping him would break a cert tier's peak coverage
            r["checked"] = False
            r["group"] = "available"
            r["hint"] = (f"peak needs only {n_target} drivers — add via Advisor "
                         f"if the day runs hot")
            r["reason"] = ""
            for t in cert_tiers:
                if t in cert_of.get(did, ()):
                    kept_cert[t] -= 1
            assignable = [d for d in assignable if d.id != did]
            avail_start.pop(did, None)
            dropped_names.append(r["driver_name"])
            checked_n -= 1
        # The banner used to spell out every tier's peak here and quote a driver count
        # taken BEFORE the solo-first pass unchecks the car-less — on 07-12 it read "19"
        # while the list showed 13. The numbers now go out structured in `capacity` (the
        # UI draws the histogram) and the driver count is settled once, after every pass.
        if dropped_names:
            swaps.insert(0, f"Left available: {', '.join(dropped_names)} — "
                            f"{checked_n} drivers covers the busiest moment.")

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
    p1_winners = set()   # dedicated-lock holders — never P3d displacement victims
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
            p1_winners.add(d.id)
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
        if peak is not None:
            # Peak basis: units of tier >= t must cover the CONCURRENT in-flight peak of
            # tier->=t legs (cumulative — exact-tier peaks under-reserve when a higher
            # tier overlaps). The descending-tier loop + ">= tier" covered-counting means
            # higher-tier reservations correctly count toward lower tiers' needs.
            need = peak["cumulative"].get(tname, (0, None))[0]
            if not need:
                continue
        else:
            need = ceil(demand[tname] / DAY_SETUP_LEGS_PER_UNIT)
        # A target the fleet cannot physically meet is not a plan, it's arithmetic about
        # bookings we will farm out. Un-capped, 07-11 asked for 19 towncar-capable and 16
        # mini_van-capable units against a fleet of 13 — four of five sizes over-asking,
        # each producing a warning no dispatcher could act on. Capping changes no
        # ASSIGNMENT (the loop already stops when the pool empties); it stops the pass
        # from reporting an impossible shortfall as a daily finding. Whether the fleet
        # covers the day at all is answered once, by parkable_units, in `capacity`.
        need = min(need, sum(1 for u in all_units if _unit_tier(u) >= tier))
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
                # Why the pass stalled decides whether it is worth saying. A free unit
                # nobody left can legally drive is a genuine, fixable, tier-specific
                # finding (the Sprinter case). Running out of UNITS or out of BODIES is
                # neither tier-specific nor per-size — the old message blamed "unit(s) of
                # that size" for both and fired up to four times a day. The settled
                # headcount line at the end says that once, accurately.
                stuck = [u for u in pool.values() if _unit_tier(u) >= tier
                         and getattr(u.vehicle_type, "requires_certification", False)]
                if stuck:
                    warnings.append(
                        f"{_unit_label(stuck[0])} is free and the day needs it, but nobody "
                        f"left on the crew is certified to drive it — tick a certified "
                        f"driver from the bench.")
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

    # P3d — FORCE-INCLUDE displacement ("Yovanny in, someone out"). A forced driver still
    # carless after P1-P3 takes the unit of the LOWEST-priority proposal of THIS run —
    # never a locked row (real DVA, founder's business; clear it in the panel first) and
    # never a P1 dedicated lock (george keeps his car). The victim steps aside exactly
    # like the solo-first path: unchecked + hinted, one click to re-add.
    if forced and unmatched:
        for fd in [d for d in list(unmatched) if d.id in forced]:
            # Prefer victims NOT holding a certification-tier unit (Sprinter): taking one
            # requires the forced driver to be certified anyway (can_drive gate keeps unit
            # + cert coverage intact), but a plain unit is always the gentler steal.
            victims = sorted(
                (vd for vd in assignable
                 if vd.id in proposed and vd.id not in forced
                 and vd.id not in p1_winners
                 and fd.can_drive(proposed[vd.id].vehicle_type)),
                key=lambda vd: (
                    1 if (proposed[vd.id].vehicle_type is not None
                          and getattr(proposed[vd.id].vehicle_type,
                                      "requires_certification", False)) else 0,
                    rank_of.get(vd.id, (0.0, 0, 0, 0))))
            if not victims:
                warnings.append(
                    f"Couldn't seat {fd}: every compatible unit is held by a locked or "
                    f"dedicated row — clear one in the vehicle panel first.")
                continue
            v = victims[0]
            u = proposed.pop(v.id)
            for r in rows:
                if r["driver_id"] == v.id:
                    r["checked"] = False
                    r["group"] = "available"
                    r["vehicle_id"] = None
                    r["vehicle_label"] = ""
                    r["reason"] = ""
                    r["hint"] = (f"stepped aside for {fd} (your pick) — add via "
                                 f"Advisor if the day needs them")
            avail_start.pop(v.id, None)
            proposed[fd.id] = u
            for r in rows:
                if r["driver_id"] == fd.id:
                    r["vehicle_id"] = u.id
                    r["vehicle_label"] = _unit_label(u)
                    r["reason"] = f"takes {_unit_label(u)} — added by you"
            unmatched.remove(fd)
            swaps.append(f"{fd} in, {v} out: {fd} takes {_unit_label(u)}; "
                         f"{v} left unchecked (lowest priority).")

    # P3c — PLANNED SHARED CARS. More checked drivers than free cars (founder: "we have
    # drivers available, more than cars — and there is no such thing as a car not working"):
    # instead of leaving a checked driver carless (his colleagues then run 15h+ days), pair
    # him onto the EARLIEST starter's unit as the PM shift. Both rows get partitioned
    # planned windows (AM: start→handoff, PM: handoff→23) which Apply persists onto the
    # vehicle rows; the auto-assign modal prefills them as HARD windows, so the engine
    # physically cannot double-book the car. Partners come only from THIS run's proposals —
    # rows the founder already set stay untouched.
    if unmatched and _solo:
        # SOLO-FIRST (demand-aware staffing): more checked drivers than cars no longer
        # auto-proposes an AM/PM share. The extras stay UNCHECKED with a hint — the
        # Second-Shift Advisor reads the actual BUILT board afterwards and proposes
        # adding them (spare or freed unit, occupancy-gated) only when the day truly
        # needs a second shift. DAY_SETUP_SOLO_FIRST=False restores the legacy
        # auto-share branch below, byte-identically.
        # A FORCED driver never gets silently unchecked — if P3d couldn't seat him he
        # stays ticked and falls through to P4's loud "No free unit" warning.
        names = [str(d) for d in sorted((x for x in unmatched if x.id not in forced),
                                        key=lambda x: str(x).lower())]
        for d in list(unmatched):
            if d.id in forced:
                continue
            for r in rows:
                if r["driver_id"] == d.id:
                    r["checked"] = False
                    r["group"] = "available"
                    r["hint"] = "available — add via Advisor if the day needs them"
                    r["reason"] = ""
            unmatched.remove(d)
        swaps.append(
            f"MORE DRIVERS THAN CARS: {', '.join(names)} left unchecked — the "
            f"Second-Shift Advisor proposes adding them after the build if the day "
            f"needs a second shift.")
    elif unmatched:
        from dispatching.models import SchedulerSettings
        sharable = sorted(((avail_start.get(pid, 4), pid) for pid in proposed),
                          key=lambda t: (t[0], t[1]))
        shared_partners = set()
        # The share-cut hour is live-editable (Build 2e): SchedulerSettings
        # share_split_hour, default 16 — the measured modal handoff hour.
        h = SchedulerSettings.get_settings().share_split_hour
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
    # ── HONEST REASON CHIPS. P2 stamped "covers <tier> demand (N legs)" on every row it
    # seated — the engine's LEAST reliable signal (right 56% of the time, vs 79% for
    # "usual unit" and 82% for "his car") worn as if it were the strongest, quoting the
    # day's TOTAL leg count so three rows read identically, and printing "covers towncar
    # demand" next to a Mini Van. Relabel with what is actually true of the pair. Done
    # here, after every pass, because "a better car is still free" is only worth saying
    # once the free pool has stopped moving.
    claims, claimed_units = [], set()
    for r in rows:
        if not (r["checked"] and str(r["reason"]).startswith("covers ")):
            continue
        d, u = by_id.get(r["driver_id"]), None
        for cand in all_units:
            if cand.id == r["vehicle_id"]:
                u = cand
                break
        if d is None or u is None:
            r["reason"] = "best fit"
            continue
        s = share(d.id, u.id)
        lu = last_unit.get(d.id)
        if lu and lu[1] == u.id:
            r["reason"] = "same car as last shift"
        elif s >= 0.3:
            r["reason"] = f"usual unit · {s:.0%}"
        else:
            here = pair_score(d, u)
            better = sorted((v for v in all_units
                             if d.can_drive(v.vehicle_type) and pair_score(d, v) > here + 10),
                            key=lambda v: -pair_score(d, v))
            free_better = [v for v in better if v.id in pool]
            if free_better:
                # The tier pass picks the UNIT first and then hunts for a driver, so it
                # can seat someone in an unfamiliar car while a car he actually drives
                # stays parked (Aug 10: ernesto → #002, 14% of his days, while #009 —
                # 27% and his last car — sat idle). Say so; the dropdown is right there.
                # Claimed below — a parked car can only be offered to ONE row.
                claims.append((pair_score(d, free_better[0]), r, free_better[0],
                               bool(better)))
                r["reason"] = "his usual cars are taken" if better else "best fit"
            elif better:
                r["reason"] = "his usual cars are taken"
            else:
                r["reason"] = "best fit"

    # Only one driver can actually take a parked car, so only one row may be told to.
    # Offering #009 to both ernesto and Francisco (as the first cut did) reads as two
    # available fixes and delivers one. Highest score wins the offer; the rest keep the
    # honest fallback already set above.
    for _s, r, v, _had in sorted(claims, key=lambda c: (-c[0], c[1]["driver_name"])):
        if v.id in claimed_units:
            continue
        claimed_units.add(v.id)
        r["reason"] = f"{_unit_label(v)} suits him better"

    rare_listed = [u for u in pool.values() if u.id in rarely_used]
    if rare_listed:
        warnings.append("Rarely-used units (never auto-suggested): "
                        + ", ".join(_unit_label(u) for u in sorted(rare_listed, key=_unit_sort_key)))

    # ── PER-ROW UNIT OPTIONS for the dropdown. The list used to be every free unit in
    # number order, identical on every row, with nothing about whether the car suited
    # the person — so overriding a pick meant knowing the history by heart. Rank it by
    # the same score the engine used, say who loses a car that is already spoken for,
    # and keep cert-blocked units out (Apply hard-blocks them server-side anyway).
    holder = {}
    for did, a in existing.items():
        if a.vehicle_id:
            holder[a.vehicle_id] = str(by_id[did]) if did in by_id else str(a.driver)
    for did, u in proposed.items():
        holder[u.id] = str(by_id[did])
    for r in rows:
        d = by_id.get(r["driver_id"])
        if d is None or r["group"] in ("locked", "off"):
            continue
        opts = []
        for u in all_units:
            if not d.can_drive(u.vehicle_type):
                continue
            s = share(d.id, u.id)
            lu = last_unit.get(d.id)
            if u.id == r["vehicle_id"]:
                note = "current pick"
            elif lu and lu[1] == u.id:
                note = "drove it last shift"
            elif s >= 0.05:
                note = f"{s:.0%} of his days"
            else:
                note = "never driven it"
            held = holder.get(u.id)
            opts.append({
                "id": u.id, "label": _unit_label(u),
                "free": u.id not in holder,
                "held_by": None if (held is None or u.id == r["vehicle_id"]) else held,
                "note": note,
                "rarely_used": u.id in rarely_used,
                "_k": (u.id != r["vehicle_id"], u.id in holder, u.id in rarely_used,
                       -pair_score(d, u)) + _unit_sort_key(u),
            })
        opts.sort(key=lambda o: o["_k"])
        for o in opts:
            o.pop("_k")
        r["unit_options"] = opts

    # ── CAN ANY CAR STAY IN? The founder's actual question, asked once. Replaces the
    # per-size "needs N unit(s) of that size" warnings (up to four a day, none
    # actionable — see the cap in P2) and the bare "Parked (unassigned) units" list.
    can_park, must_run = (parkable_units(all_units, peak["cumulative"],
                                         overall=peak["overall"][0])
                          if peak is not None else ([], list(all_units)))
    idle = sorted(pool.values(), key=_unit_sort_key)
    capacity = {
        "fleet": len(all_units),
        "staffed": len(all_units) - len(idle),
        "idle": [{"id": u.id, "label": _unit_label(u)} for u in idle],
        "can_park": [{"id": u.id, "label": _unit_label(u)}
                     for u in sorted(can_park, key=_unit_sort_key)],
        "must_run": len(must_run),
        "series": concurrency_series(target_date, legs),
    }
    if peak is not None:
        if can_park:
            swaps.append(
                "Quiet day — " + ", ".join(_unit_label(u) for u in
                                           sorted(can_park, key=_unit_sort_key))
                + f" can stay in; the other {len(must_run)} still cover every trip.")
        elif idle:
            swaps.append(f"Every car is needed today, but {len(idle)} "
                         f"({', '.join(_unit_label(u) for u in idle)}) has no driver.")
        # The real shortfall, settled AFTER every pass: not "fewer drivers than the raw
        # peak" (which counts bookings we farm out and fired on 9 of 23 days), but
        # "fewer drivers than the cars this day genuinely needs on the road".
        final_checked = sum(1 for r in rows if r["checked"])
        if final_checked < len(must_run):
            warnings.append(
                f"The busiest moment needs {len(must_run)} cars out but only "
                f"{final_checked} drivers are ticked — add {len(must_run) - final_checked} "
                f"more from the bench, or the gap farms out.")

    # ── ORDER: by UNIT number inside each group (#001, #002, ... #10, #11, ... #17),
    # not by driver name. Founder's rule — the fleet is the fixed thing he reads down,
    # and the yard, the board and the printed sheet are all in unit order, so an
    # alphabetical modal forced him to re-map 13 names onto 17 cars every morning.
    # _unit_sort_key is numeric-aware, so #10 sorts after #009, never before #002.
    # Rows with no car keep their alphabetical order, at the end of their group.
    group_order = {"locked": 0, "suggested": 1, "available": 2, "off": 3}
    unit_key = {u.id: _unit_sort_key(u) for u in all_units}
    rows.sort(key=lambda r: (
        group_order[r["group"]],
        0 if r.get("vehicle_id") in unit_key else 1,
        unit_key.get(r.get("vehicle_id"), (0, 0, "")),
        r["driver_name"].lower(),
    ))
    # ── Build-2 split-shift extras: ADDITIVE payload keys only (shared_units /
    # mint_proposals / span_exceptions / standby_pool + per-row span_hours).
    # Any failure degrades to the classic payload — never breaks the modal. ──
    extras = {}
    try:
        extras = _split_shift_extras(target_date, legs, all_units, oos_units,
                                     proposed, drivers, by_id, rows)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Day Setup split-shift extras failed")

    payload = {
        "date": target_date.isoformat(),
        "rows": rows,
        "swaps": swaps,
        "warnings": warnings,
        "demand": dict(sorted(demand.items(), key=lambda kv: -get_vehicle_tier(kv[0]))),
        "peak": ({"overall": {"n": peak["overall"][0],
                              "at": peak["overall"][1].strftime("%H:%M")
                                    if peak["overall"][1] else ""},
                  "per_tier": {t: {"n": n, "at": at.strftime("%H:%M")}
                               for t, (n, at) in peak["per_tier"].items()}}
                 if peak is not None else None),
        "capacity": capacity,
        "free_units": [
            {"id": u.id, "label": _unit_label(u),
             "requires_cert": bool(u.vehicle_type and u.vehicle_type.requires_certification),
             "rarely_used": u.id in rarely_used}
            for u in sorted(pool.values(), key=_unit_sort_key)
        ],
        # snapshot for the apply view's drift check (409 if a row changed since preview)
        "snapshot": {str(a.driver_id): a.vehicle_id for a in existing.values()},
    }
    payload.update(extras)
    return payload


def _vnum(vid, units):
    for u in units:
        if u.id == vid:
            return u.vehicle_number
    return "?"


# ═════════════════════════════════════════════════════════════════════════════
# SPLIT SHIFTS & HANDOFFS (scheduling redesign, Build 2) — payload extras
# ═════════════════════════════════════════════════════════════════════════════
# Additive keys on the suggest payload: `shared_units` (green/amber/red handoff
# feasibility per shared car, 2b), `mint_proposals` (standby second shifts, 2c),
# `span_exceptions` (the priced crunch exception, 2d), `standby_pool`, plus a
# per-row `span_hours` readout. Existing keys untouched. Propose-only: nothing
# here writes — a ticked proposal goes through apply_day_setup like any pair.
#
# The pool rule and the mint engine are dispatching/standby_mints.py — the
# SHARED core extracted from analysis/10 (verified byte-identical), so the
# panel's feasibility verdicts cannot drift from the evidence replay. The zone
# chain and the band rule are dispatching/handoff_chain.py. No drive-time
# lookups on unknown routes happen here (the chain is a static table): the
# billed INLINE_RESOLVER path is untouched.

def _fmt_clock(dt_):
    return dt_.strftime("%I:%M %p").lstrip("0")


def _split_shift_extras(target_date, legs, all_units, oos_units,
                        proposed, drivers, by_id, rows):
    """Build the Build-2 payload keys. Read-only; estimates from booked times."""
    from datetime import datetime as _dt

    from dispatching import feasibility_guards as fg
    from dispatching import handoff_chain as hc
    from dispatching import standby_mints as sm
    from dispatching.analytics import categorize_location
    from dispatching.models import SchedulerSettings
    from drivers.models import Driver, DriverDateOverride, DriverVehicleAssignment
    from reservations.models import Leg

    cfg = SchedulerSettings.get_settings()
    unit_by_id = {u.id: u for u in all_units}
    for u in oos_units:
        unit_by_id.setdefault(u.id, u)

    # ── The day's legs as the mint engine sees them (P50 occupancy — the
    # replay convention), plus the zone/type side-tables the bands need. ──
    leg_zone_pick, leg_zone_drop, leg_kind = {}, {}, {}
    day_mls = []
    for l in legs:
        if l.pickup_time is None:
            continue
        pcat = categorize_location(l.pickup_location)
        dcat = categorize_location(l.dropoff_location)
        kind = hc.occupancy_kind(pcat, dcat)
        # effective_vehicle_type is already the RAW vehicle_type string (never
        # the .title()-cased __str__ — the _unit_tier gotcha), same field the
        # replay's SQL COALESCE reads.
        tier = sm.VEHICLE_TIER.get(l.effective_vehicle_type, sm.VEHICLE_TIER_DEFAULT)
        ml = sm.MintLeg(l.id, target_date, _dt.combine(target_date, l.pickup_time),
                        kind, l.driver_id, tier)
        leg_zone_pick[l.id], leg_zone_drop[l.id], leg_kind[l.id] = pcat, dcat, kind
        day_mls.append(ml)

    drv_meta = {d.id: d for d in Driver.objects.filter(
        id__in={ml.did for ml in day_mls if ml.did is not None})}
    for d in drivers:
        drv_meta.setdefault(d.id, d)

    boards, farmed = {}, []
    for ml in day_mls:
        d = drv_meta.get(ml.did) if ml.did is not None else None
        if d is not None and d.driver_type == "inhouse":
            boards.setdefault(ml.did, []).append(ml)
        elif d is None or d.driver_type == "affiliate":
            farmed.append(ml)   # unassigned counts with farmed: it walks out too
    for ls in boards.values():
        ls.sort(key=lambda x: x.pick)

    # ── Roster/fleet state, RAW like the replay: real DVA rows only. This
    # run's not-yet-applied proposals deliberately do NOT count — a mint rides
    # only on a car that is actually on the day's plan, exactly the world the
    # evidence replay validated (Apply the roster first; the proposals appear
    # on the next Suggest). ``proposed`` stays a parameter so a later build
    # can revisit that choice consciously. ──
    dva_rows = list(DriverVehicleAssignment.objects
                    .filter(date=target_date, vehicle__isnull=False)
                    .select_related("driver", "driver__profile"))
    dva_day = {r.driver_id: r.vehicle_id for r in dva_rows}
    from drivers.models import FleetVehicle
    fleet, oos_ids = {}, set()
    for u in FleetVehicle.objects.all().select_related("vehicle_type"):
        raw = u.vehicle_type.vehicle_type if u.vehicle_type else None
        fleet[u.id] = {"active": bool(u.is_active),
                       "tier": sm.VEHICLE_TIER.get(raw, sm.VEHICLE_TIER_DEFAULT)}
        if u.is_out_of_service_on(target_date):
            oos_ids.add(u.id)
        unit_by_id.setdefault(u.id, u)

    def name_of(did):
        d = drv_meta.get(did) or by_id.get(did)
        return str(d) if d is not None else f"driver {did}"

    # ── Rest bounds vs the ACTUAL adjacent boards (03 §1 rule 3) ──
    rest_min = cfg.rest_min_gap_minutes
    prev_day, next_day = (target_date - timedelta(days=1),
                          target_date + timedelta(days=1))
    prev_end, next_start = {}, {}
    adj = (Leg.objects.filter(pickup_date__in=(prev_day, next_day),
                              driver__isnull=False, pickup_time__isnull=False,
                              driver__driver_type="inhouse")
           .exclude(reservation__status__in=("cancelled", "canceled"))
           .exclude(status="cancelled"))
    for l in adj:
        kind = hc.occupancy_kind(categorize_location(l.pickup_location),
                                 categorize_location(l.dropoff_location))
        ml = sm.MintLeg(l.id, l.pickup_date,
                        _dt.combine(l.pickup_date, l.pickup_time), kind,
                        l.driver_id, 0)
        if l.pickup_date == prev_day:
            if l.driver_id not in prev_end or ml.end > prev_end[l.driver_id]:
                prev_end[l.driver_id] = ml.end
        else:
            if l.driver_id not in next_start or ml.start < next_start[l.driver_id]:
                next_start[l.driver_id] = ml.start

    def rest_ok_first(did, _day, first_start):
        b = prev_end.get(did)
        return b is None or (first_start - b).total_seconds() / 60.0 >= rest_min

    def rest_ok_last(did, _day, last_end):
        b = next_start.get(did)
        return b is None or (b - last_end).total_seconds() / 60.0 >= rest_min

    # ── The adopted standby pool (shared rule; `drivers` is already the
    # active in-house, demo-excluded roster — a strict subset of the replay's
    # candidates, so the panel can only propose FEWER people, never more) ──
    off_today = set()
    for o in DriverDateOverride.objects.filter(status="approved",
                                               exception_type="off",
                                               date__lte=target_date):
        a, b = o.date, (o.end_date or o.date)
        if b < a or (b - a).days > 90:   # same malformed-row guard as the replay
            b = a
        if a <= target_date <= b:
            off_today.add(o.driver_id)
    works_today = {did for did, ls in boards.items() if ls}
    pool = sm.standby_pool_ids([d.id for d in drivers], works_today, dva_day,
                               off_today)

    out = {
        "standby_pool": [{"id": did, "name": name_of(did)} for did in pool],
        "shared_units": [], "mint_proposals": [], "span_exceptions": [],
        "premium_per_leg": sm.FARMOUT_PREMIUM_PER_LEG,
    }

    # ── Per-row span readout (2d): the driver's planned day vs 13.5 h ──
    hard_cap = float(cfg.span_exception_max_hours)
    for r in rows:
        ls = boards.get(r["driver_id"])
        if not ls:
            continue
        sp = sm.span_h(ls)
        r["span_hours"] = round(sp, 1)
        r["span_state"] = ("over_hard" if sp > hard_cap
                          else "over_soft" if sp > sm.SPAN_CAP_H else "")

    # ── Handoff band helper (2b): A hands the unit to B ──
    def band_for(a_last, b_first):
        gap_min = (b_first.pick - a_last.end).total_seconds() / 60.0
        b = hc.handoff_band(
            leg_zone_drop[a_last.id], leg_zone_pick[b_first.id], gap_min,
            incoming_is_arrival=(b_first.kind == "ARRIVAL"),
            green_pct=cfg.handoff_gap_green_pct,
            amber_floor_pct=cfg.handoff_gap_amber_floor_pct)
        ready = a_last.end + timedelta(
            minutes=hc.car_ready_min(leg_zone_drop[a_last.id])[1])
        return b, _fmt_clock(ready)

    # ── 2b: every shared car already on the plan (locked rows + this run) ──
    by_unit = {}
    for did, vid in dva_day.items():
        by_unit.setdefault(vid, []).append(did)
    for vid, dids in sorted(by_unit.items()):
        if len(dids) < 2:
            continue
        entry = {
            "vehicle_id": vid,
            "vehicle_label": _unit_label(unit_by_id[vid]) if vid in unit_by_id else f"#{vid}",
            "drivers": [{"id": x, "name": name_of(x)} for x in sorted(dids)],
            "handoff_band": None, "handoff_ready_at": None,
            "handoff_reason": "", "from_driver": None, "to_driver": None,
        }
        with_legs = [x for x in dids if boards.get(x)]
        if len(with_legs) >= 2:
            a, b = sorted(with_legs, key=lambda x: boards[x][0].pick)[:2]
            a_last, b_first = boards[a][-1], boards[b][0]
            if b_first.pick > a_last.pick:
                band, ready = band_for(a_last, b_first)
                entry.update({
                    "handoff_band": band["band"], "handoff_ready_at": ready,
                    "handoff_reason": band["reason"],
                    "from_driver": name_of(a), "to_driver": name_of(b),
                })
            else:
                entry["handoff_reason"] = ("jobs interleave — no single handoff "
                                           "moment (estimated from booked times)")
        else:
            entry["handoff_reason"] = ("no jobs on one side yet — feasibility "
                                       "appears once the day is built")
        out["shared_units"].append(entry)

    # ── 2c/2d: the mint engine + the crunch exception, on BUILT days only.
    # Mints are a day-before decision read off the built board (01 §2); on a
    # date with nothing assigned in-house yet there is no board to read. ──
    if not boards:
        return out
    gap = cfg.vehicle_share_pad_min
    res = sm.replay_one_day(
        target_date, boards, farmed, dva_day, fleet, pool,
        gap=gap, buf=sm.MINT_BUF_CENTRAL_MIN, cap_h=sm.SPAN_CAP_H,
        policy="soft", rest_ok_first=rest_ok_first, rest_ok_last=rest_ok_last,
        is_oos=lambda v: v in oos_ids)

    farmed_ids = {ml.id for ml in farmed}
    for m in res["mints"]:
        mls = m["legs"]
        first, last = mls[0], mls[-1]
        partner_ids = [x for x in by_unit.get(m["veh"], [])
                       if x in dva_day and dva_day[x] == m["veh"] and x != m["driver"]]
        partner_id = partner_ids[0] if partner_ids else None
        partner_board = boards.get(partner_id) or []

        band_d = {"band": None, "reason": "no handoff — the unit has no other "
                                          "jobs planned that day"}
        ready = None
        if m["side"] == "late" and partner_board:
            band_d, ready = band_for(partner_board[-1], first)
        elif m["side"] == "early" and partner_board:
            band_d, ready = band_for(last, partner_board[0])
        if band_d.get("band") == "red":
            continue   # red is shown on existing shares, never SUGGESTED (2b)

        # Suggested AM/PM cut for the planned windows (2a): the hour the car
        # actually changes hands; the dispatcher can edit it after Apply.
        if m["side"] == "late":
            cut = first.pick.hour
            mint_w = (cut, 23)
            partner_w = (4, max(cut - 1, 0))
        elif m["side"] == "early":
            cut = partner_board[0].pick.hour if partner_board else min(last.pick.hour + 2, 23)
            mint_w = (first.pick.hour, max(cut - 1, 0))
            partner_w = (cut, 23)
        elif first.pick.hour >= 12:
            cut = first.pick.hour
            mint_w = (cut, 23)
            partner_w = (4, max(cut - 1, 0))
        else:
            cut = min(last.pick.hour + 2, 23)
            mint_w = (first.pick.hour, max(cut - 1, 0))
            partner_w = (cut, 23)

        n_farm = sum(1 for ml in mls if ml.id in farmed_ids)
        legs_out = []
        for ml in mls:
            src = ("farm" if (ml.id in farmed_ids and ml.did is not None)
                   else "unassigned" if ml.id in farmed_ids else "shed")
            legs_out.append({"id": ml.id, "time": _fmt_clock(ml.pick),
                             "kind": ml.kind.lower(), "source": src,
                             "from_driver": (name_of(ml.did)
                                             if src != "unassigned" else None)})
        out["mint_proposals"].append({
            "driver_id": m["driver"], "driver_name": name_of(m["driver"]),
            "vehicle_id": m["veh"],
            "vehicle_label": (_unit_label(unit_by_id[m["veh"]])
                              if m["veh"] in unit_by_id else f"#{m['veh']}"),
            "side": m["side"],
            "window": f"{_fmt_clock(first.pick)} – {_fmt_clock(last.end)}",
            "planned_start_hour": mint_w[0], "planned_end_hour": mint_w[1],
            "partner_driver_id": partner_id,
            "partner_name": name_of(partner_id) if partner_id else None,
            "partner_planned_start_hour": partner_w[0],
            "partner_planned_end_hour": partner_w[1],
            "cut_hour": cut,
            "legs": legs_out, "n_jobs": len(mls),
            "est_saving": round(n_farm * sm.FARMOUT_PREMIUM_PER_LEG),
            "thin": len(mls) < cfg.mint_min_jobs_soft,
            "handoff_band": band_d.get("band"),
            "handoff_ready_at": ready,
            "handoff_reason": band_d.get("reason", ""),
        })

    # ── 2d: the priced crunch exception. Residual pool legs the capped day
    # couldn't reach: would extending ONE driver past 13.5 h (never past the
    # hard ceiling) keep them in-house? Same engine, higher cap, no mints —
    # so every share/buffer/rest rule still holds. Rendered as a choice. ──
    placed = {ml.id for m in res["mints"] for ml in m["legs"]}
    for did, ls in res["boards"].items():
        placed.update(ml.id for ml in ls)
    residual = [ml for ml in farmed if ml.id not in placed]
    if residual and hard_cap > sm.SPAN_CAP_H:
        # The crunch pass must SEE the proposed mints: fold each mint in as its
        # driver's board (with the roster row implied), so an extension can
        # never overlap or crowd a second shift proposed above. Mint drivers
        # themselves never get an exception — a fresh call-out is not a crunch.
        boards2 = dict(res["boards"])
        dva2 = dict(dva_day)
        mint_driver_ids = set()
        for m in res["mints"]:
            boards2[m["driver"]] = list(m["legs"])
            dva2.setdefault(m["driver"], m["veh"])
            mint_driver_ids.add(m["driver"])
        res2 = sm.replay_one_day(
            target_date, boards2, residual, dva2, fleet, [],
            gap=gap, buf=sm.MINT_BUF_CENTRAL_MIN, cap_h=hard_cap,
            policy="free", no_mint=True,
            rest_ok_first=rest_ok_first, rest_ok_last=rest_ok_last,
            is_oos=lambda v: v in oos_ids)
        for did in sorted(res2["boards"]):
            if did in mint_driver_ids:
                continue
            before, after = boards2.get(did, []), res2["boards"][did]
            added = [ml for ml in after if ml.id not in {x.id for x in before}]
            if not added:
                continue
            base_sp, new_sp = sm.span_h(before), sm.span_h(after)
            if new_sp <= sm.SPAN_CAP_H:
                continue   # not an exception — the builder can just take it
            out["span_exceptions"].append({
                "driver_id": did, "driver_name": name_of(did),
                "base_span": round(base_sp, 1), "new_span": round(new_sp, 1),
                "added_hours": round(new_sp - base_sp, 1),
                "legs": [{"id": ml.id, "time": _fmt_clock(ml.pick)}
                         for ml in added],
                "n_legs": len(added),
                "est_value": round(len(added) * sm.FARMOUT_PREMIUM_PER_LEG),
            })
    return out
