"""Second-Shift Advisor (Span Governor Phase 5) — "this day needs another driver."

Read-only, preview-only. After the full auto-assign pipeline (build -> swap -> rescue ->
trim -> gap passes) it looks at what's left that no amount of reshuffling can fix:
  (a) RESIDUAL legs no working driver can take, and
  (b) drivers still over the 13.5h-effective target whose tail the trim pass couldn't move,
clusters them into one-driver-sized SHIFT PROPOSALS, and names a concrete in-house source
for each: an idle driver + a spare unit, or an idle driver taking a FREED unit (a working
driver's car after he clears — the founder's historical AM/PM share pattern, 34 instances).

Accepting a proposal goes through the modal + the Day Setup apply endpoint (real
DriverVehicleAssignment row, validated/atomic) — the engine then routes the work through its
existing machinery on the next preview: residual proposals seed via Build-1st; overload
proposals drain via the span-trim pass. The advisor never assigns legs itself and never
farms anything: a proposal with no in-house source degrades to today's Need Affiliates
behavior, explained.

Design + adversarial review: docs/scheduler-automation/auto-assign-hour-balancing-design.md
PART 2 (must-fixes applied: MIN_LEGS=1, freed-unit source in v1, tier-aware clustering,
threshold = fg.SPAN_SOFT_EFFECTIVE_HOURS strict-greater, mid-shift/demo drivers excluded
from the idle roster). Tight same-unit handoffs (vehicle_handoff_ok) remain Phase 4; the
freed-unit source requires a WIDE buffer instead.
"""
from datetime import datetime, timedelta, date

# ── Flags ────────────────────────────────────────────────────────────────────
ADVISOR_ENABLED = True
ADVISOR_MIN_LEGS = 1            # all measured second-shift slots are ONE-leg tails
ADVISOR_CLUSTER_GAP_MIN = 180   # max idle inside one proposal (else it splits)
ADVISOR_CHAIN_PAD_MIN = 20      # min turnaround assumed between chained proposal legs
ADVISOR_FREED_BUFFER_MIN = 90   # a freed unit needs holder's clear + this before the
                                # proposal's first pickup (wide buffer — no Phase-4 gate yet)
ADVISOR_MAX_PROPOSALS = 4
ADVISOR_SUGGEST_SCHEDULED_OFF = True   # off-per-schedule idle drivers may be suggested,
                                       # loudly labeled — the dispatcher decides


def build_shift_proposals(target_date: date, residual_legs, overload_map,
                          working_ids, schedules_by_driver, legs_by_id):
    """Build advisor proposals. Pure read (no writes).

    residual_legs: leg objects still unassigned after all passes.
    overload_map: {driver_id: {"name", "slots" (sorted ScheduleSlots),
                   "movable_ids" (set: this run's unlocked auto-assigned leg ids)}}
        — drivers still over the effective target after the trim pass.
    working_ids: driver ids working today (the modal's authoritative set).
    schedules_by_driver: the PROPOSED board {driver_id: DriverDaySchedule} (for freed-unit
        clear times).
    legs_by_id: {leg_id: Leg} for the date.
    """
    if not ADVISOR_ENABLED:
        return []
    from dispatching import feasibility_guards as fg
    from dispatching.scheduler import estimate_job_end_time, get_vehicle_tier
    from dispatching.day_setup import (_is_excluded, _unit_tier, _unit_label,
                                       DAY_SETUP_MIN_UNIT_DAYS, DAY_SETUP_HISTORY_DAYS)
    from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle

    # ── Trigger legs ──
    trigger = []   # (leg, kind, overload_driver_name)
    _locked_info = []
    for leg in residual_legs:
        trigger.append((leg, "residual", None))
    for did, info in (overload_map or {}).items():
        # Min-tail: the smallest suffix of his MOVABLE legs whose removal brings him under
        # the target. Largest-gap splitting is wrong when the day's big gap is in the
        # morning — the measured overflows are all evening tails.
        slots = sorted(info["slots"], key=lambda s: s.pickup_time)
        movable = info.get("movable_ids") or set()
        suffix = []
        for s in reversed(slots):
            if s.leg_id not in movable:
                break
            suffix.insert(0, s)
            remaining = slots[:len(slots) - len(suffix)]
            if not remaining:
                suffix.pop(0)
                break
            first = datetime.combine(target_date, remaining[0].pickup_time)
            last = max(x.estimated_end_time for x in remaining)
            if (last - first).total_seconds() / 3600 <= fg.SPAN_SOFT_EFFECTIVE_HOURS:
                break
        if not suffix:
            # Tail is hand-assigned/locked: nothing the engine may move. Say so instead of
            # staying silent (founder: "rizwan 3:45 AM-6:15 PM is too long" — if his tail is
            # pinned, the fix is the dispatcher's to make, so name it).
            _locked_info.append(
                f"{info['name']} runs long, but his late legs are hand-assigned/locked — "
                f"unassign one (✗) and rebuild to let a second shift take it.")
            continue
        for s in suffix:
            leg = legs_by_id.get(s.leg_id)
            if leg is not None:
                trigger.append((leg, "overload", info["name"]))
    def _info_cards():
        return [{"signature": f"_locked{i}", "kind": "info", "leg_count": 0, "text": t}
                for i, t in enumerate(_locked_info)]

    if not trigger:
        return _info_cards()

    # ── Tier-aware clustering: group by exact demanded tier, chain within tier by time ──
    by_tier = {}
    for leg, kind, src in trigger:
        tname = str(leg.effective_vehicle_type or "")
        by_tier.setdefault(tname, []).append((leg, kind, src))
    clusters = []
    for tname, items in sorted(by_tier.items(), key=lambda kv: -get_vehicle_tier(kv[0] or "towncar")):
        items.sort(key=lambda t: (t[0].pickup_time, t[0].id))
        cur = []
        cur_end = None
        for leg, kind, src in items:
            pu = datetime.combine(target_date, leg.pickup_time)
            end = estimate_job_end_time(leg, target_date)
            if cur and (pu < cur_end + timedelta(minutes=ADVISOR_CHAIN_PAD_MIN)
                        or (pu - cur_end) > timedelta(minutes=ADVISOR_CLUSTER_GAP_MIN)):
                clusters.append((tname, cur))
                cur, cur_end = [], None
            cur.append((leg, kind, src))
            cur_end = max(cur_end, end) if cur_end else end
        if cur:
            clusters.append((tname, cur))
    clusters = [(t, c) for t, c in clusters if len(c) >= ADVISOR_MIN_LEGS]
    if not clusters:
        return []

    # ── Sources ──
    history_start = target_date - timedelta(days=DAY_SETUP_HISTORY_DAYS)
    unit_usage = {}
    for vid, d in (DriverVehicleAssignment.objects
                   .filter(date__gte=history_start, date__lt=target_date,
                           vehicle__isnull=False)
                   .values_list("vehicle_id", "date")):
        unit_usage.setdefault(vid, set()).add(d)
    todays = list(DriverVehicleAssignment.objects.filter(date=target_date)
                  .select_related("vehicle", "vehicle__vehicle_type", "driver"))
    assigned_unit_holder = {a.vehicle_id: a.driver_id for a in todays if a.vehicle_id}
    units = {u.id: u for u in FleetVehicle.objects.filter(is_active=True).select_related("vehicle_type")}
    spare_units = [u for uid, u in sorted(units.items())
                   if uid not in assigned_unit_holder
                   and len(unit_usage.get(uid, ())) >= DAY_SETUP_MIN_UNIT_DAYS]

    # Idle roster: active in-house, not working, not excluded, and WITHOUT legs today
    # (a driver with legs but no vehicle row is mid-shift data drift, not idle).
    busy_ids = set(working_ids or [])
    for did, info in (overload_map or {}).items():
        busy_ids.add(did)
    from reservations.models import Leg
    has_legs = set(Leg.objects.filter(pickup_date=target_date, driver__isnull=False)
                   .exclude(status="cancelled").values_list("driver_id", flat=True))
    idle = []
    for d in (Driver.objects.filter(driver_type="inhouse", is_active=True)
              .select_related("profile")
              .prefetch_related("certified_vehicle_types", "weekly_schedule", "date_overrides")):
        if d.id in busy_ids or d.id in has_legs or _is_excluded(d):
            continue
        eff = d.get_effective_availability(target_date)
        if not eff["is_available"] and not ADVISOR_SUGGEST_SCHEDULED_OFF:
            continue
        idle.append((d, bool(eff["is_available"])))
    idle.sort(key=lambda t: (not t[1], t[0].id))   # available-today first, then id

    def freed_units_for(first_pickup_dt):
        """Units whose holder clears comfortably before the proposal starts."""
        out = []
        for a in todays:
            if not a.vehicle_id:
                continue
            sched = schedules_by_driver.get(a.driver_id)
            if sched is None or not sched.slots:
                continue
            clear = max(s.estimated_end_time for s in sched.slots)
            if clear + timedelta(minutes=ADVISOR_FREED_BUFFER_MIN) <= first_pickup_dt:
                out.append((clear, a.vehicle_id, str(a.driver)))
        out.sort(key=lambda t: (t[0], t[1]))
        return out

    proposals = []
    for tname, items in clusters:
        legs = [t[0] for t in items]
        kinds = {t[1] for t in items}
        overload_from = sorted({t[2] for t in items if t[2]})
        first_pu = datetime.combine(target_date, min(l.pickup_time for l in legs))
        last_end = max(estimate_job_end_time(l, target_date) for l in legs)
        tier = get_vehicle_tier(tname or "towncar")
        revenue = sum(float(getattr(l, "revenue_share", 0) or 0) for l in legs)

        # Candidate (driver, unit, freed_info) combos.
        options = []
        for d, avail_today in idle:
            for u in spare_units:
                if _unit_tier(u) >= tier and d.can_drive(u.vehicle_type):
                    options.append((0 if avail_today else 1, 0, _unit_tier(u) - tier,
                                    d.id, d, u, None))
            for clear, vid, holder in freed_units_for(first_pu):
                u = units[vid]
                if _unit_tier(u) >= tier and d.can_drive(u.vehicle_type):
                    options.append((0 if avail_today else 1, 1, _unit_tier(u) - tier,
                                    d.id, d, u,
                                    {"holder": holder, "clear": clear.strftime("%I:%M %p").lstrip("0")}))
        options.sort(key=lambda o: (o[0], o[1], o[2], o[3], _unit_tier(o[5])))

        def _opt_json(o):
            _, is_freed, _, _, d, u, freed = o
            effav = d.get_effective_availability(target_date)
            return {
                "driver_id": d.id, "driver_name": str(d),
                "vehicle_id": u.id, "vehicle_label": _unit_label(u),
                "scheduled_off": not effav["is_available"],
                "freed": freed,   # {holder, clear} when sharing a working driver's car
                "start_hour": max(0, first_pu.hour - 1),
                "end_hour": min(23, last_end.hour + 1),
            }

        best = _opt_json(options[0]) if options else None
        alternates = []
        seen = {(best["driver_id"], best["vehicle_id"])} if best else set()
        for o in options[1:]:
            j = _opt_json(o)
            k = (j["driver_id"], j["vehicle_id"])
            if k in seen:
                continue
            seen.add(k)
            alternates.append(j)
            if len(alternates) >= 3:
                break

        proposals.append({
            "signature": "-".join(str(l.id) for l in sorted(legs, key=lambda x: x.id)),
            "kind": "overload" if kinds == {"overload"} else "residual",
            "overload_from": overload_from,
            "tier_label": tname or "any",
            "leg_count": len(legs),
            "window": f"{first_pu.strftime('%I:%M %p').lstrip('0')}–"
                      f"{last_end.strftime('%I:%M %p').lstrip('0')}",
            "revenue": round(revenue),
            "legs": [{"leg_id": l.id,
                      "pickup": l.pickup_time.strftime("%I:%M %p").lstrip("0"),
                      "route": f"{(l.pickup_location or '')[:25]} → {(l.dropoff_location or '')[:25]}"}
                     for l in sorted(legs, key=lambda x: (x.pickup_time, x.id))],
            "best": best,
            "alternates": alternates,
        })

    # Highest-value proposals first; deterministic. FAIRNESS RESERVATION: high-revenue
    # coverage (residual) cards must not crowd out every overload card — long days are the
    # founder's stated pain, so the best overload card always keeps a seat.
    proposals.sort(key=lambda p: (-p["revenue"], p["signature"]))
    kept = proposals[:ADVISOR_MAX_PROPOSALS]
    if (len(proposals) > ADVISOR_MAX_PROPOSALS
            and not any(p["kind"] == "overload" for p in kept)):
        best_overload = next((p for p in proposals if p["kind"] == "overload"), None)
        if best_overload is not None:
            kept = kept[:-1] + [best_overload]
    dropped = len(proposals) - len(kept)
    proposals = kept
    if dropped > 0:
        proposals.append({"signature": "_more", "kind": "info", "leg_count": 0,
                          "text": f"+{dropped} more potential shift(s) not shown — resolve these first."})
    proposals.extend(_info_cards())
    return proposals
