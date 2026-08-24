"""Handoff-chain configuration — the version-controlled structured tables from
the scheduling redesign (docs/scheduling-redesign/03_STANDBY_AND_HANDOFF_MODEL.md).

POSTURE: STRICTLY READ-ONLY DATA + pure helpers. No model writes, no HTTP, no
ORM. Anything that needs these tables imports them from here — they are never
duplicated at a call site (04 §1 rule 4).

What lives here, by build:
  * Build 1 (shipped): the fitted occupancy lead/tail table — how long a leg
    really occupies a driver/car around its booked pickup time. Used by the
    manual-assign co-driver car-share check (assign_warnings.py).
  * Build 2 (this build): the zone chain (drop zone -> wash -> fuel -> base ->
    next-pickup zone, low/central/high), the green/amber/red handoff
    feasibility rule (03 §3.2) and the flight-volatility guard (03 §3.3).

Every number is labeled with its source per 00's convention.
"""

# ════════════════════════════════════════════════════════════════════════════
# OCCUPANCY LEAD/TAIL — minutes around the booked pickup_time
# ════════════════════════════════════════════════════════════════════════════
# [measured — 00 §A3.5] Fitted on 12,354 legs carrying BOTH the on-the-way and
# completed taps, so lead and tail come from the same legs (fitting them on
# different subsets inflates the interval — a trap 00 hit and corrected).
# A leg occupies its driver (and car) over:
#
#     [pickup_time - lead(kind), pickup_time + tail(kind)]
#
# Convention (00 §A3.5): the P50 pair is for AGGREGATE staffing arithmetic
# (summed across many concurrent legs, where per-leg conservatism compounds
# into a peak nobody has ever had to cover); the P75 pair is for SINGLE-LEG
# FEASIBILITY decisions, where conservatism belongs.
#
# Kind is the conflict-family classifier: airport PICKUP -> ARRIVAL, airport
# DROPOFF -> DEPARTURE, else OTHER (see occupancy_kind below). Airport->airport
# counts as ARRIVAL — the leg starts at a gate, so it inherits arrival dwell.

OCCUPANCY_LEAD_TAIL_P50 = {           # {kind: (lead_min, tail_min)}
    "ARRIVAL":   (20.6, 75.5),
    "DEPARTURE": (36.3, 34.8),
    "OTHER":     (39.8, 53.6),
}

OCCUPANCY_LEAD_TAIL_P75 = {           # {kind: (lead_min, tail_min)}
    "ARRIVAL":   (48.1, 90.2),
    "DEPARTURE": (55.0, 43.8),
    "OTHER":     (62.0, 76.5),
}


def occupancy_kind(pickup_category, dropoff_category):
    """ARRIVAL / DEPARTURE / OTHER from the shipped location categories.

    Categories come from ``dispatching.analytics.categorize_location`` — the
    same classifier the drive-time table is keyed on, so the occupancy family
    and the turnaround family can never disagree about what a leg is.
    Airport->airport is ARRIVAL (gate start -> arrival dwell); ``get_trip_type``
    has a hole here and is deliberately not used.
    """
    from dispatching import feasibility_guards as fg
    if pickup_category in fg.AIRPORT_TERMINALS:
        return "ARRIVAL"
    if dropoff_category in fg.AIRPORT_TERMINALS:
        return "DEPARTURE"
    return "OTHER"


def occupancy_interval(pickup_dt, kind, percentile="p75"):
    """(start, end) datetimes the leg occupies its driver/car.

    ``percentile``: "p50" for aggregate staffing arithmetic, "p75" (default)
    for a single-leg feasibility decision — per the 00 §A3.5 convention.
    """
    from datetime import timedelta
    table = (OCCUPANCY_LEAD_TAIL_P50 if percentile == "p50"
             else OCCUPANCY_LEAD_TAIL_P75)
    lead, tail = table.get(kind, table["OTHER"])
    return (pickup_dt - timedelta(minutes=lead),
            pickup_dt + timedelta(minutes=tail))


# ════════════════════════════════════════════════════════════════════════════
# THE ZONE CHAIN — drop → wash → fuel → base → next pickup (03 §3.1)
# ════════════════════════════════════════════════════════════════════════════
# The founder's base handoff process (D7): outgoing driver drops the last guest
# → El Car Wash by MCO → fuel → base at 6785 Narcoossee Rd → incoming driver
# (waiting at base) takes the car → drives to their first pickup. Every handoff
# is modeled through the base — no house-handoff modeling, no west-side
# shortcut (03 §3.2 simplification, founder-supplied).
#
# Components are (low, central, high) minutes. Fuel is the founder's closed
# number — 8 minutes flat (03 §6, 2026-08-23); the evidence script
# analysis/11 and its committed 11_chain_matrix.csv predate that closure and
# carried fuel as [assumed 5/7.5/10], so washed-chain values here sit
# (+3.0 / +0.5 / −2.0) min from that CSV; skip-wash floors are identical.

CHAIN_COMPONENTS = {                  # {step: (low, central, high) minutes}
    "mco_to_wash":  (14.0, 15.5, 17.0),   # [founder-supplied]
    "wash":         (15.0, 17.5, 20.0),   # [founder-supplied] El Car Wash, by MCO
    "fuel":         (8.0, 8.0, 8.0),      # [founder-supplied — closed 2026-08-23]
    "wash_to_base": (20.0, 20.0, 20.0),   # [founder-supplied ~20, point estimate]
    "mco_to_base":  (12.0, 12.0, 12.0),   # [founder-supplied]
}

# base (6785 Narcoossee, SR-528/Narcoossee corridor ~12 min E of MCO) → pickup
# zone, (low, central, high) minutes. Zone names are the shipped
# ``dispatching.analytics.categorize_location`` vocabulary — the same
# classifier the drive-time table is keyed on.
BASE_TO_ZONE = {
    "MCO Terminal":        (12, 12, 12),  # [founder-supplied]
    "SFB Terminal":        (55, 60, 65),  # [shipped-estimate MCO<->SFB 60 ± base offset]
    "Disney Resort":       (30, 35, 40),  # [founder-supplied ~40 high; shipped 30 low]
    "Universal Resort":    (25, 32, 40),  # [founder-supplied ~40 high; shipped 25 low]
    "Port Canaveral Area": (45, 50, 55),  # [assumed — base sits ON SR-528 E of MCO, < MCO's 55]
    "Airport Hotel":       (12, 15, 18),  # [shipped-estimate 12 + base offset]
    "Other Hotel":         (25, 28, 32),  # [shipped-estimate 25 + base offset]
    "Residential":         (30, 33, 37),  # [shipped-estimate 30 + base offset]
    "Other":               (35, 38, 42),  # [shipped-estimate DEFAULT 35 + base offset]
}

# An arrival's booked time IS its flight time and moves, mostly in-day. A
# handoff whose incoming first job is an airport arrival must survive this
# retime at GREEN and is flagged at AMBER beyond it (03 §3.3).
FLIGHT_RETIME_P75_MIN = 13            # [measured — P75 |retime|, 00 §A3.8]


def _drive_to_mco(zone):
    """Shipped planning drive minutes zone → MCO Terminal (scheduler table)."""
    from dispatching.scheduler import DEFAULT_DRIVE_TIME, DRIVE_TIME_ESTIMATES
    return DRIVE_TIME_ESTIMATES.get((zone, "MCO Terminal"), DEFAULT_DRIVE_TIME)


def _base_to(zone):
    return BASE_TO_ZONE.get(zone, BASE_TO_ZONE["Other"])


def pickup_buffer_min(pickup_zone):
    """Pre-pickup buffer: airport pickups need the driver in the terminal by
    gate+grace; elsewhere the passenger-ready convention (pickup_policy —
    [shipped-estimate, production convention])."""
    from dispatching import feasibility_guards as fg, pickup_policy
    if pickup_zone in fg.AIRPORT_TERMINALS:
        return pickup_policy.ARRIVAL_MEET_GRACE_MIN
    return pickup_policy.PAX_READY_MIN


def car_ready_min(drop_zone):
    """(low, central, high) minutes after the guest is dropped until the car is
    washed, fueled and back at base. Non-MCO drops route via the MCO corridor:
    shipped(zone → MCO) + the founder MCO→wash range (wash and base both sit
    by the airport — founder-confirmed geography, 03 §6)."""
    c = CHAIN_COMPONENTS
    if drop_zone == "MCO Terminal":
        to_wash = c["mco_to_wash"]
    else:
        d = _drive_to_mco(drop_zone)
        to_wash = tuple(d + x for x in c["mco_to_wash"])
    return tuple(to_wash[i] + c["wash"][i] + c["fuel"][i] + c["wash_to_base"][i]
                 for i in range(3))


def clear_to_pickup_min(drop_zone, pickup_zone):
    """The full chain, (low, central, high): guest dropped → car ready at base
    → drive to the incoming driver's first pickup zone → pre-pickup buffer."""
    cr, bt = car_ready_min(drop_zone), _base_to(pickup_zone)
    bf = pickup_buffer_min(pickup_zone)
    return tuple(cr[i] + bt[i] + bf for i in range(3))


def skip_wash_floor_min(drop_zone, pickup_zone):
    """The AMBER fast path, central minutes: drop → base DIRECT (no wash, no
    fuel) → pickup. ≈34 min for an MCO→MCO pass — the tightest handoff ever
    executed sits on this path (03 §3.2)."""
    c = CHAIN_COMPONENTS
    direct = (c["mco_to_base"][1] if drop_zone == "MCO Terminal"
              else _drive_to_mco(drop_zone) + c["mco_to_base"][1])
    return direct + _base_to(pickup_zone)[1] + pickup_buffer_min(pickup_zone)


# ════════════════════════════════════════════════════════════════════════════
# GREEN / AMBER / RED — the handoff feasibility rule (03 §3.2 + §3.3)
# ════════════════════════════════════════════════════════════════════════════

def handoff_band(drop_zone, pickup_zone, clear_to_pickup_gap_min,
                 incoming_is_arrival=False, green_pct=100, amber_floor_pct=100):
    """Band one proposed handoff on one car.

    ``clear_to_pickup_gap_min``: minutes from the outgoing driver's CLEAR time
    (last booked pickup + the P50 occupancy tail — the same arithmetic the
    measured calibration in analysis/11 used, where 75% of real handoffs clear
    the central bar) to the incoming driver's first booked pickup.

    ``green_pct`` / ``amber_floor_pct``: live-editable scalars
    (SchedulerSettings ``handoff_gap_green_pct`` / ``handoff_gap_amber_floor_pct``)
    applied to the central and low chains respectively; 100/100 reproduces
    03 §3.2 exactly.

    Returns {"band", "reason", "need_central", "need_low", "fast_path"}:
      GREEN — clears the central chain (plus the P75 flight retime when the
              incoming first job is an airport arrival, 03 §3.3);
      AMBER — below central but ≥ the low chain, or on the skip-wash fast
              path: feasible ONLY with an explicit dispatcher plan (wash the
              evening before / hand off at MCO);
      RED   — below the low chain with no fast path. Not plannable; a red
              share is shown, never suggested.
    """
    lo, ce, _hi = clear_to_pickup_min(drop_zone, pickup_zone)
    need_green = ce * green_pct / 100.0
    need_amber = lo * amber_floor_pct / 100.0
    fast = skip_wash_floor_min(drop_zone, pickup_zone)
    gap = clear_to_pickup_gap_min
    guard = FLIGHT_RETIME_P75_MIN if incoming_is_arrival else 0

    if gap >= need_green + guard:
        return {"band": "green", "need_central": need_green, "need_low": need_amber,
                "fast_path": fast,
                "reason": (f"clears the full wash-fuel-base chain "
                           f"({gap:.0f} min vs {need_green:.0f} needed"
                           + (f", incl. the {guard}-min flight-retime guard" if guard else "")
                           + "; estimated from booked times)")}
    if gap >= need_amber or gap >= fast:
        plan = ("wash the evening before, or hand off directly at MCO"
                if gap < need_amber else "wash the evening before")
        vol = (" First job is a flight arrival — the time can move; leave slack."
               if incoming_is_arrival else "")
        return {"band": "amber", "need_central": need_green, "need_low": need_amber,
                "fast_path": fast,
                "reason": (f"tight: {gap:.0f} min vs {need_green:.0f} for the full "
                           f"chain — needs an explicit plan: {plan}.{vol}")}
    return {"band": "red", "need_central": need_green, "need_low": need_amber,
            "fast_path": fast,
            "reason": (f"infeasible: {gap:.0f} min is under the {need_amber:.0f}-min "
                       f"minimum chain and under the {fast:.0f}-min skip-wash floor")}
