"""The co-driver car-share gate — one home (Build 3a, P2).

WHAT THIS IS
------------
When two drivers hold ONE physical vehicle on one date, something has to decide
whether a proposed job is physically possible: the car cannot be in two places,
it cannot change hands more often than a human can drive it to base, and it
cannot be handed over faster than the wash/fuel/base chain allows. That
decision used to be written out in several files at once. It lives here now:
the engine, the manual-assign warnings, the mint engine and the replay scripts
all call into this module.

    from dispatching.car_share import build_sharer_partners, sharers_conflict

``scheduler`` and ``assign_warnings`` re-export the names they used to define,
so every existing caller and both replay scripts keep working unchanged.

POSTURE: importable WITHOUT Django. Module level is stdlib only; the ORM,
``dispatching.scheduler`` and ``dispatching.models`` are imported lazily inside
the functions that need them. That is the same contract ``handoff_chain.py``
and ``standby_mints.py`` hold, and it is what lets the analysis scripts import
this without booting Django.

════════════════════════════════════════════════════════════════════════════
THE THREE CONVENTIONS — READ THIS BEFORE CHANGING ANYTHING
════════════════════════════════════════════════════════════════════════════
There is one gate, but it is evaluated under three DIFFERENT interval
conventions, and they are not interchangeable. Build 3a deliberately did NOT
collapse them: each one flips at least one verdict on real inputs, so the
choice is the founder's, not a refactor's (04 §1 — deviations go back for
review). They are gathered here so the disagreement is visible in one file
instead of spread across four.

  A. ENGINE GATE — ``sharers_conflict``.
     Interval: ``[pickup − pad, estimate_job_end_time + pad]`` for the
     candidate, against the partner's RAW slot ``[pickup, estimated_end_time]``.
     No pre-pickup lead — the pad IS the lead. The engine clock is
     flight-aware. Rules: overlap only, no interleave rule.
     Verdict: bool; every caller treats True as a HARD skip (the leg goes to
     the farm pool). Pad: ``SchedulerSettings.engine_share_pad_min`` (65) — a
     DEDICATED dial, split from B/C's ``vehicle_share_pad_min`` on 2026-08-24.
     Reason for the split: this test measures its pad from the candidate's
     CLEAR time forward, so the SAME numeric pad is a materially stricter
     requirement here than B/C's pickup-to-pickup measurement (§9.1/9.2 in
     05_BUILD3B_TICKETS.md) — at the shared 120 it was rejecting, and
     therefore farming out, real handoffs the founder confirmed ran fine
     against actual operating history, one as tight as a 48-minute
     clear-to-pickup gap. Tune it alone; it never touches B or C.

  B. MANUAL-PATH WARNINGS — ``share_conflicts``.
     Interval: ``handoff_chain`` occupancy lead/tail at **P75** (the
     single-leg feasibility convention, 00 §A3.5). Rules: overlap,
     interleave (at most one hand-back per unit-day), and a
     pickup-to-pickup pad. Verdict: a list of codes, ADVISORY — the manual
     path never blocks (assign_warnings.CLASS_SEVERITY).

  C. MINT ENGINE — ``mint_share_ok``.
     Interval: the same table at **P50** (the aggregate-arithmetic
     convention — the replay places many concurrent legs). Rules: overlap
     AND full one-sided separation (a driver's pickups sit entirely ≥ gap
     before the co-driver's first pickup or ≥ gap after their last), i.e.
     no interleaving at all. Verdict: bool; a False kills the proposal.

Worked disagreements, on inputs that occur:

  * An MCO arrival booked 10:00 dropping at a Disney resort, partner holding
    one job picking up 07:30 and clearing 09:00. A's block (pad 120) starts
    08:00 → CONFLICT, the builder farms the leg. B's P75 block starts 09:11.9
    and the pickup gap is 150 ≥ 120 → no warning at all on the manual path.
  * Partner holds 06:00 (clears 07:00) and 18:00; candidate at 12:00. A
    allows it (no interleave rule) and the builder will hand over the midday
    job. C rejects it — the car would change hands three times. B fires
    ``share_interleave`` as an info row.
  * Two OTHER legs on one unit at 09:00 and 11:00. At P50 the blocks miss and
    the mint engine accepts; at P75 they overlap and the manual path warns.

Forcing a single convention therefore either (i) removes assignments the
builder produces today, or (ii) re-admits the physically impossible unit-days
the adversarial replay caught (the +4.0 → +2.4 legs/day correction recorded in
``standby_mints``). Both are founder calls. What IS unified below — the
overlap predicate, the occupancy-interval construction, the holders grouping
and the ≤2 constant — changes no verdict anywhere.

The rest of the layering, for orientation (not implemented here):
``handoff_chain.handoff_band`` prices a handoff's geography green/amber/red;
``day_setup``'s planned AM/PM window is hour-granular and pad-free and
explicitly delegates safety to convention A; ``views.apply_day_setup`` is the
only place the ≤2 rule is actually enforced against a write.
"""

# ════════════════════════════════════════════════════════════════════════════
# THE ONE CONSTANT
# ════════════════════════════════════════════════════════════════════════════

# At most two drivers per vehicle-date. Never observed above 2 in either
# regime [measured, 03 §2]; enforced server-side at the write door
# (views.apply_day_setup) and respected by the mint engine when it opens a
# second shift. NOTE: the engine gate and the manual warnings do not know this
# rule at all, and shift_advisor can still offer a unit that already has two
# holders — see the Build 3a report, that is an open founder decision.
MAX_DRIVERS_PER_VEHICLE_DATE = 2


# ════════════════════════════════════════════════════════════════════════════
# SHARED PRIMITIVES — identical in every copy, so unifying changes no verdict
# ════════════════════════════════════════════════════════════════════════════

def intervals_overlap(a_start, a_end, b_start, b_end):
    """Half-open intersection: do [a_start, a_end) and [b_start, b_end) meet?

    This exact expression was written out in six places (the engine gate, the
    warning core, and four spots in the mint engine) with no boundary
    difference between them. Touching ends do NOT overlap — a job clearing at
    10:00 and one starting at 10:00 are compatible.
    """
    return a_start < b_end and b_start < a_end


def holders_by_unit(pairs):
    """{vehicle_id: [driver_id, ...]} — who holds each car on the date.

    ``pairs`` is an iterable of (driver_id, vehicle_id); rows with no vehicle
    are skipped. Insertion order is preserved, which is what every caller
    relied on before this was extracted.

    The FILTER stays at the call site on purpose. The four callers scope their
    rows differently and those differences are semantic, not accidental: the
    engine keeps only drivers in the caller's working set, the manual path
    keeps every DVA row for the date, and the write door additionally requires
    an active in-house driver. Folding any of those into this helper would
    change behaviour — see the module docstring.
    """
    units = {}
    for did, vid in pairs:
        if vid is None:
            continue
        units.setdefault(vid, []).append(did)
    return units


def occupancy_block(pickup_dt, kind, percentile):
    """(start, end) the leg occupies its car — the one occupancy construction.

    Thin pass-through to ``handoff_chain.occupancy_interval`` so the lead/tail
    table has exactly one reader. ``percentile`` is REQUIRED and never
    defaulted here: P50 for aggregate placement arithmetic, P75 for a
    single-leg feasibility call (00 §A3.5). Which one a caller wants is a
    modelling decision, not something this module should guess.
    """
    from dispatching.handoff_chain import occupancy_interval
    return occupancy_interval(pickup_dt, kind, percentile=percentile)


# ════════════════════════════════════════════════════════════════════════════
# PARTNER RESOLUTION
# ════════════════════════════════════════════════════════════════════════════

def build_sharer_partners(driver_ids, target_date, rows=None):
    """Map {driver_id: {other drivers sharing the SAME physical vehicle that date}}.

    Built from DriverVehicleAssignment: a vehicle held by >1 working driver is one
    physical unit split across shifts (Day Setup AM/PM share or an advisor freed-unit
    accept). Feed the result to sharers_conflict() to gate any insert against the
    car-share partner's jobs. Pass ``rows`` (the date's DVA rows — real, prefetched,
    or a Build-3 candidate plan's UNSAVED rows) to skip the query.

    Scope caveat, load-bearing: only drivers inside ``driver_ids`` become
    partners. A co-holder the caller did not include — an inactive driver, or
    one with no legs when the caller derived its set from legs rather than the
    roster — makes the gate silently return {} for that unit.
    """
    from drivers.models import DriverVehicleAssignment

    working = set(driver_ids)
    if rows is None:
        rows = DriverVehicleAssignment.objects.filter(
            date=target_date, vehicle__isnull=False, driver_id__in=working)
    unit_holders = holders_by_unit(
        (dva.driver_id, dva.vehicle_id) for dva in rows
        if dva.driver_id in working)
    partners = {}
    for holders in unit_holders.values():
        if len(holders) > 1:
            for did in holders:
                partners.setdefault(did, set()).update(
                    h for h in holders if h != did)
    return partners


# ════════════════════════════════════════════════════════════════════════════
# CONVENTION A — the engine gate (hard; removes work from a build)
# ════════════════════════════════════════════════════════════════════════════

def sharers_conflict(leg, driver_id, sharer_partners, schedules, target_date,
                     pad_min=None):
    """True if giving `leg` to `driver_id` would overlap his car-share PARTNER's jobs
    (one physical unit). Interval = [pickup - pad, est_end + pad] vs every partner slot.
    schedules: {driver_id: DriverDaySchedule} for the CURRENT board state."""
    from datetime import datetime, timedelta

    from dispatching.scheduler import estimate_job_end_time

    partners = (sharer_partners or {}).get(driver_id)
    if not partners:
        return False
    if pad_min is None:
        # engine_share_pad_min, NOT vehicle_share_pad_min — separate dial since
        # 2026-08-24. This convention measures its pad from the candidate's own
        # CLEAR time (see below), not pickup-to-pickup, so it is a materially
        # stricter test of the SAME 120-min figure than conventions B/C apply —
        # strict enough that it was farming out real handoffs the founder
        # ground-truthed as fine (one at a 48-min clear-to-pickup gap). See
        # car_share.py's module docstring and 05_BUILD3B_TICKETS.md §9.1/9.2.
        from dispatching.models import SchedulerSettings
        pad_min = SchedulerSettings.get_settings().engine_share_pad_min
    pad = timedelta(minutes=pad_min)
    start = datetime.combine(target_date, leg.pickup_time) - pad
    end = estimate_job_end_time(leg, target_date) + pad
    for pid in partners:
        psched = schedules.get(pid)
        if psched is None:
            continue
        for s in psched.slots:
            s_start = datetime.combine(target_date, s.pickup_time)
            if intervals_overlap(start, end, s_start, s.estimated_end_time):
                return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# CONVENTION B — the manual-path warnings (advisory; never blocks)
# ════════════════════════════════════════════════════════════════════════════

def build_share_entry(leg_id, did, pickup_dt, pickup_category, dropoff_category):
    """One ``share_conflicts`` entry from a leg's booked pickup + categories.
    Shared by the endpoint path and the replay (same P75 feasibility blocks)."""
    from dispatching.handoff_chain import occupancy_kind
    start, end = occupancy_block(
        pickup_dt, occupancy_kind(pickup_category, dropoff_category),
        percentile="p75")
    return {"leg_id": leg_id, "did": did, "pick": pickup_dt,
            "start": start, "end": end}


def share_conflicts(entries, pad_min, focus_leg_id=None):
    """PURE decision core of the co-driver car-share check — shared verbatim by
    the manual-assign endpoint (``assign_warnings``) and the precision replay
    (docs/scheduling-redesign/analysis/12_warn_precision.py), so script and
    product cannot drift.

    ``entries``: one shared unit-day as
    ``[{"leg_id", "did", "pick", "start", "end"}, ...]`` — occupancy blocks per
    leg (handoff_chain lead/tail around the booked pickup ``pick``). Need not be
    sorted. ``focus_leg_id`` scopes the verdicts to conflicts a specific leg
    creates (the endpoint's "warn about THIS assignment" contract); None scores
    the whole unit-day (the replay).

    Returns a list of {"code", "a", "b"} dicts:
      * ``share_overlap``    a/b cross-driver entries whose blocks intersect —
                             one physical car in two places;
      * ``share_pad``        adjacent cross-driver pickups (by pickup order)
                             whose pickup-to-pickup gap is under ``pad_min``,
                             skipped where the same pair already overlaps;
      * ``share_interleave`` (a=b=None) the unit's day switches drivers more
                             than once — with a focus leg, only when THAT leg
                             creates the extra switch.
    """
    entries = sorted(entries, key=lambda e: (e["pick"], e["leg_id"]))
    out = []

    # Overlap: cross-driver occupancy blocks that intersect.
    overlapped_pairs = set()
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a["did"] == b["did"]:
                continue
            if focus_leg_id is not None and focus_leg_id not in (a["leg_id"], b["leg_id"]):
                continue
            if intervals_overlap(a["start"], a["end"], b["start"], b["end"]):
                overlapped_pairs.add((a["leg_id"], b["leg_id"]))
                out.append({"code": "share_overlap", "a": a, "b": b})

    # Interleave: at most one hand-back per unit-day.
    def switches(seq):
        ids = [e["did"] for e in seq]
        return sum(1 for x, y in zip(ids, ids[1:]) if x != y)

    with_all = switches(entries)
    if focus_leg_id is not None:
        without = switches([e for e in entries if e["leg_id"] != focus_leg_id])
        if with_all > max(1, without):
            out.append({"code": "share_interleave", "a": None, "b": None})
    elif with_all > 1:
        out.append({"code": "share_interleave", "a": None, "b": None})

    # Handoff pad: adjacent cross-driver pickups must clear the pad; an
    # overlapping pair already fired the harder verdict above.
    for a, b in zip(entries, entries[1:]):
        if a["did"] == b["did"]:
            continue
        if focus_leg_id is not None and focus_leg_id not in (a["leg_id"], b["leg_id"]):
            continue
        if (a["leg_id"], b["leg_id"]) in overlapped_pairs:
            continue
        gap = int((b["pick"] - a["pick"]).total_seconds() / 60)
        if gap < pad_min:
            out.append({"code": "share_pad", "a": a, "b": b})
    return out


# ════════════════════════════════════════════════════════════════════════════
# CONVENTION C — the mint gate (hard; kills a second-shift proposal)
# ════════════════════════════════════════════════════════════════════════════

def mint_share_ok(new_block, partner_blocks, gap_min):
    """STRICT car sharing for a proposed mint leg — the rule the fixed-strict
    replay enforces (``standby_mints``, extracted byte-identically from
    analysis/10).

    ``new_block``      the candidate leg, an object with .pick/.start/.end.
    ``partner_blocks`` every block the co-driver(s) on the same car already
                       hold — roster board plus mints opened this run.
    ``gap_min``        minimum pickup-to-pickup separation (the live
                       ``vehicle_share_pad_min``, default 120).

    Two conditions, both required: the candidate's occupancy may not overlap
    any co-driver block, AND its pickup must sit entirely ≥ gap before their
    FIRST pickup or ≥ gap after their LAST — no interleaving at all. The
    one-sided rule is what an earlier lenient replay lacked when it booked one
    car in two places; the adversarial verifier's correction (+4.0 → +2.4
    legs/day) is the reason it is strict.
    """
    if not partner_blocks:
        return True
    for ol in partner_blocks:
        if intervals_overlap(new_block.start, new_block.end, ol.start, ol.end):
            return False
    pmin = min(l.pick for l in partner_blocks)
    pmax = max(l.pick for l in partner_blocks)
    lo = (pmin - new_block.pick).total_seconds() / 60.0
    hi = (new_block.pick - pmax).total_seconds() / 60.0
    return lo >= gap_min or hi >= gap_min
