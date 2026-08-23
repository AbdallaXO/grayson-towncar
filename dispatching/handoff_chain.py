"""Handoff-chain configuration — the version-controlled structured tables from
the scheduling redesign (docs/scheduling-redesign/03_STANDBY_AND_HANDOFF_MODEL.md).

POSTURE: STRICTLY READ-ONLY DATA + pure helpers. No model writes, no HTTP, no
ORM. Anything that needs these tables imports them from here — they are never
duplicated at a call site (04 §1 rule 4).

What lives here, by build:
  * Build 1 (shipped): the fitted occupancy lead/tail table — how long a leg
    really occupies a driver/car around its booked pickup time. Used by the
    manual-assign co-driver car-share check (assign_warnings.py).
  * Build 2 (planned): the zone chain matrix (drop zone -> wash -> fuel -> base
    -> next-pickup zone, low/central/high) and the green/amber/red handoff
    feasibility rule from 03 §3.1-§3.2.

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
